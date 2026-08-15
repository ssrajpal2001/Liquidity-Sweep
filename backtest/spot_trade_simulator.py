"""
backtest/spot_trade_simulator.py

Simulates trading NIFTY SPOT ITSELF (not options) for backtest reporting,
since 2 years of historical option premium data isn't available. This is
a DIFFERENT P&L model than execution/risk_engine.py's Spot-Risk x Delta
formula — that formula exists specifically to size an OPTION position;
trading spot directly has no delta involved at all, so this module does
NOT use risk_engine.py. Quantity is a flat lot size (65, per NIFTY's
current lot size) rather than a computed options quantity.

Trade management, per the user's explicit instructions:
  - Entry: the retest candle's close (SignalDecision.entry_candle.close)
  - SL: SignalDecision.spot_structural_sl
  - Target 1: entry +/- (risk_points * target1_rr), 50% booked, SL moves
    to breakeven on the remainder
  - Target 2: entry +/- (risk_points * target2_rr) — no broker-supplied
    "opposing HTF zone" is computed here since that would need forward-
    looking rolling-base data; target2_rr is a simple, disclosed multiple
    instead (default 3.0x risk), clearly distinguishable in the report
    from a "real" structural target.
  - Walks forward candle-by-candle through the SAME LTF candles the
    signal came from, checking SL/Target1/Target2 per candle
    (conservative: if both an SL and a target level fall inside the same
    candle's range, SL is assumed hit first — worst case, not best case).
  - "Stale" outcome: candle history runs out before any exit level is
    hit (i.e. still open at the end of the backtest window).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from data_feed.candle_aggregator import Candle
from strategy.state_machine import SignalDecision

DEFAULT_TARGET1_RR = 1.5
DEFAULT_TARGET2_RR = 3.0
DEFAULT_TARGET1_BOOKING_PCT = 50  # per the user's explicit "50% partial" instruction


class TradeOutcome(str, Enum):
    TARGET1_THEN_TARGET2 = "T2 Hit"
    TARGET1_THEN_SL = "T1 Hit, BE Stop"  # booked T1, then stopped at breakeven (not a loss, ~0 on runner)
    SL_HIT_DIRECT = "SL Hit"             # stopped out before ever reaching Target 1
    STALE = "Stale"                       # never resolved within available candle history


@dataclass
class SimulatedTrade:
    signal: SignalDecision
    entry_price: float
    entry_time: datetime
    sl_price: float
    target1_price: float
    target2_price: float
    lot_size: int

    outcome: TradeOutcome = TradeOutcome.STALE
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    target1_hit_time: Optional[datetime] = None
    points_pnl: float = 0.0
    rupee_pnl: float = 0.0
    rr_achieved: float = 0.0


def simulate_spot_trade(
    signal: SignalDecision,
    subsequent_ltf_candles: list[Candle],
    lot_size: int = 65,
    target1_rr: float = DEFAULT_TARGET1_RR,
    target2_rr: float = DEFAULT_TARGET2_RR,
    target1_booking_pct: int = DEFAULT_TARGET1_BOOKING_PCT,
) -> SimulatedTrade:
    entry_price = signal.entry_candle.close
    entry_time = signal.entry_candle.open_time
    sl_price = signal.spot_structural_sl
    risk_points = abs(entry_price - sl_price)
    is_long = signal.direction == "bullish"  # CE-equivalent: profits as spot rises

    target1_price = entry_price + risk_points * target1_rr if is_long else entry_price - risk_points * target1_rr
    target2_price = entry_price + risk_points * target2_rr if is_long else entry_price - risk_points * target2_rr

    trade = SimulatedTrade(
        signal=signal, entry_price=entry_price, entry_time=entry_time, sl_price=sl_price,
        target1_price=target1_price, target2_price=target2_price, lot_size=lot_size,
    )

    current_sl = sl_price
    target1_booked = False

    for candle in subsequent_ltf_candles:
        if candle.open_time <= entry_time:
            continue  # only walk forward candles strictly after entry

        if not target1_booked:
            hit_sl = candle.low <= current_sl if is_long else candle.high >= current_sl
            hit_t1 = candle.high >= target1_price if is_long else candle.low <= target1_price
            if hit_sl and hit_t1:
                hit_t1 = False  # conservative tie-break: SL wins if both in range same candle
            if hit_sl:
                trade.outcome = TradeOutcome.SL_HIT_DIRECT
                trade.exit_price = current_sl
                trade.exit_time = candle.open_time
                break
            if hit_t1:
                target1_booked = True
                current_sl = entry_price  # move to breakeven
                trade.target1_hit_time = candle.open_time
                continue

        else:
            hit_sl = candle.low <= current_sl if is_long else candle.high >= current_sl
            hit_t2 = candle.high >= target2_price if is_long else candle.low <= target2_price
            if hit_sl and hit_t2:
                hit_t2 = False
            if hit_t2:
                trade.outcome = TradeOutcome.TARGET1_THEN_TARGET2
                trade.exit_price = target2_price
                trade.exit_time = candle.open_time
                break
            if hit_sl:
                trade.outcome = TradeOutcome.TARGET1_THEN_SL
                trade.exit_price = current_sl
                trade.exit_time = candle.open_time
                break

    _compute_pnl(trade, target1_booking_pct, is_long, entry_price, risk_points)
    return trade


def _compute_pnl(
    trade: SimulatedTrade, target1_booking_pct: int, is_long: bool, entry_price: float, risk_points: float
) -> None:
    lot_size = trade.lot_size
    sign = 1 if is_long else -1
    t1_frac = target1_booking_pct / 100
    remaining_frac = 1 - t1_frac

    if trade.outcome == TradeOutcome.SL_HIT_DIRECT:
        points = sign * (trade.exit_price - entry_price)
        trade.points_pnl = points
        trade.rupee_pnl = points * lot_size
        trade.rr_achieved = points / risk_points if risk_points else 0.0

    elif trade.outcome == TradeOutcome.TARGET1_THEN_SL:
        t1_points = sign * (trade.target1_price - entry_price)
        remainder_points = sign * (trade.exit_price - entry_price)  # exit at breakeven -> ~0
        blended_points = t1_frac * t1_points + remaining_frac * remainder_points
        trade.points_pnl = blended_points
        trade.rupee_pnl = blended_points * lot_size
        trade.rr_achieved = blended_points / risk_points if risk_points else 0.0

    elif trade.outcome == TradeOutcome.TARGET1_THEN_TARGET2:
        t1_points = sign * (trade.target1_price - entry_price)
        t2_points = sign * (trade.target2_price - entry_price)
        blended_points = t1_frac * t1_points + remaining_frac * t2_points
        trade.points_pnl = blended_points
        trade.rupee_pnl = blended_points * lot_size
        trade.rr_achieved = blended_points / risk_points if risk_points else 0.0

    else:  # STALE — an open/unresolved trade has no realized P&L to report.
        trade.points_pnl = 0.0
        trade.rupee_pnl = 0.0
        trade.rr_achieved = 0.0
