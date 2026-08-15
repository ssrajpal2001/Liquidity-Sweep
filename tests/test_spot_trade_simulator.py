from __future__ import annotations

from datetime import datetime, timezone

from backtest.spot_trade_simulator import TradeOutcome, simulate_spot_trade
from data_feed.candle_aggregator import Candle
from strategy.displacement import DisplacementDirection, FairValueGap
from strategy.state_machine import SignalDecision
from strategy.sweep_detector import SweepDirection, SweepEvent


def _candle(o, h, l, c, minute):
    return Candle(
        instrument_key="NIFTY", timeframe_minutes=3,
        open_time=datetime(2026, 8, 10, 9, minute, tzinfo=timezone.utc),
        open=o, high=h, low=l, close=c,
    )


def _bullish_signal(entry_close=25000, sl=24980):
    entry_candle = _candle(25000, 25005, 24995, entry_close, 15)
    sweep = SweepEvent(direction=SweepDirection.BULLISH, level=24950, candle=entry_candle, pierce_points=2)
    fvg = FairValueGap(direction=DisplacementDirection.BULLISH, gap_high=25010, gap_low=25000,
                        formed_at_candle_open_time="x")
    return SignalDecision(instrument_key="NIFTY", direction="bullish", sweep=sweep, fvg=fvg,
                           entry_candle=entry_candle, spot_structural_sl=sl)


def test_target1_then_target2_blended_pnl_correct():
    signal = _bullish_signal()
    candles = [_candle(25000, 25035, 24995, 25030, 18), _candle(25030, 25065, 25025, 25060, 21)]
    trade = simulate_spot_trade(signal, candles, lot_size=65)

    assert trade.outcome == TradeOutcome.TARGET1_THEN_TARGET2
    assert trade.points_pnl == 45.0   # 0.5*30 + 0.5*60
    assert trade.rupee_pnl == 2925.0  # 45 * 65
    assert trade.rr_achieved == 2.25  # 45 / 20 risk points


def test_direct_sl_hit_before_target1():
    signal = _bullish_signal()
    candles = [_candle(25000, 25010, 24975, 24980, 18)]
    trade = simulate_spot_trade(signal, candles, lot_size=65)

    assert trade.outcome == TradeOutcome.SL_HIT_DIRECT
    assert trade.points_pnl == -20.0
    assert trade.rupee_pnl == -1300.0


def test_target1_then_breakeven_stop():
    signal = _bullish_signal()
    candles = [_candle(25000, 25035, 24995, 25030, 18), _candle(25030, 25032, 24995, 25000, 21)]
    trade = simulate_spot_trade(signal, candles, lot_size=65)

    assert trade.outcome == TradeOutcome.TARGET1_THEN_SL
    assert trade.points_pnl == 15.0  # 0.5*30 + 0.5*0
    assert trade.target1_hit_time is not None


def test_stale_when_no_exit_level_reached():
    signal = _bullish_signal()
    candles = [_candle(25000, 25010, 24995, 25005, 18)]
    trade = simulate_spot_trade(signal, candles, lot_size=65)

    assert trade.outcome == TradeOutcome.STALE
    assert trade.points_pnl == 0.0
    assert trade.exit_price is None


def test_bearish_direction_mirrors_correctly():
    entry_candle = _candle(25000, 25005, 24995, 25000, 15)
    sweep = SweepEvent(direction=SweepDirection.BEARISH, level=25050, candle=entry_candle, pierce_points=2)
    fvg = FairValueGap(direction=DisplacementDirection.BEARISH, gap_high=25000, gap_low=24990,
                        formed_at_candle_open_time="x")
    signal = SignalDecision(instrument_key="NIFTY", direction="bearish", sweep=sweep, fvg=fvg,
                             entry_candle=entry_candle, spot_structural_sl=25020)  # risk = 20

    # Price falls to Target1 (25000 - 30 = 24970) then Target2 (25000 - 60 = 24940)
    candles = [_candle(25000, 25005, 24965, 24970, 18), _candle(24970, 24975, 24935, 24940, 21)]
    trade = simulate_spot_trade(signal, candles, lot_size=65)

    assert trade.outcome == TradeOutcome.TARGET1_THEN_TARGET2
    assert trade.points_pnl == 45.0  # profits as price falls, same magnitude as bullish case
    assert trade.rupee_pnl == 2925.0


def test_conservative_tie_break_when_sl_and_target_in_same_candle():
    """If a single wide candle's range covers both SL and Target1, the
    simulator must assume SL was hit first (worst case), not silently
    assume the best-case fill order."""
    signal = _bullish_signal()
    # One huge candle spanning both the SL (24980) and Target1 (25030) range.
    candles = [_candle(25000, 25040, 24970, 25010, 18)]
    trade = simulate_spot_trade(signal, candles, lot_size=65)

    assert trade.outcome == TradeOutcome.SL_HIT_DIRECT  # conservative assumption, not T1
