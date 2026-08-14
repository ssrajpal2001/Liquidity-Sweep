"""
strategy/displacement.py

Two checks that must both pass, in order, after a SweepEvent:

1. Displacement / Market Structure Shift (MSS/CHoCH): the candle(s)
   immediately after the sweep must show a sharp, opposite-direction
   impulse — body size > ATR_MULTIPLIER x ATR (config: risk.atr_
   displacement_multiplier, default 1.5) AND it must break the local
   micro-swing structure.

2. Fair Value Gap (FVG): the displacement candle must leave a 3-candle
   imbalance (candle[i-1].low > candle[i+1].high for a bullish FVG, or
   candle[i-1].high < candle[i+1].low for a bearish FVG) — this is the
   zone Phase 4's retest_trigger.py waits for a pullback into.

Both are pure functions over a short candle window, so both are fully
unit-testable with synthetic data.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from data_feed.candle_aggregator import Candle


def average_true_range(candles: list[Candle]) -> float:
    """Simple ATR over the given window (no smoothing) — candles must be
    in chronological order. Uses high-low range as a proxy for true range
    since we don't track a separate previous-close series here; adequate
    for the body-size-vs-volatility comparison this module needs."""
    if not candles:
        return 0.0
    ranges = [c.high - c.low for c in candles]
    return sum(ranges) / len(ranges)


def body_size(candle: Candle) -> float:
    return abs(candle.close - candle.open)


class DisplacementDirection(str, Enum):
    BULLISH = "bullish"  # reversal UP after a bullish (low) sweep
    BEARISH = "bearish"  # reversal DOWN after a bearish (high) sweep


@dataclass
class DisplacementEvent:
    direction: DisplacementDirection
    candle: Candle
    body_to_atr_ratio: float


def detect_displacement(
    candidate_candle: Candle,
    lookback_candles: list[Candle],
    micro_swing_high: float,
    micro_swing_low: float,
    atr_multiplier: float = 1.5,
) -> Optional[DisplacementEvent]:
    """`lookback_candles` = the N candles immediately BEFORE
    candidate_candle (used only for ATR), NOT including it.
    `micro_swing_high`/`low` = the local structure the displacement candle
    must break through (from the swing just before the sweep)."""
    atr = average_true_range(lookback_candles)
    if atr <= 0:
        return None

    ratio = body_size(candidate_candle) / atr
    if ratio <= atr_multiplier:
        return None

    is_bullish_body = candidate_candle.close > candidate_candle.open
    if is_bullish_body and candidate_candle.close > micro_swing_high:
        return DisplacementEvent(DisplacementDirection.BULLISH, candidate_candle, ratio)
    if not is_bullish_body and candidate_candle.close < micro_swing_low:
        return DisplacementEvent(DisplacementDirection.BEARISH, candidate_candle, ratio)
    return None


@dataclass
class FairValueGap:
    direction: DisplacementDirection
    gap_high: float
    gap_low: float
    formed_at_candle_open_time: str


def detect_fvg(
    candle_before: Candle, displacement_candle: Candle, candle_after: Candle
) -> Optional[FairValueGap]:
    """Classic 3-candle imbalance check, centered on the displacement
    candle. candle_after must already be closed to confirm the gap didn't
    get immediately filled/negated."""
    if candle_before.low > candle_after.high:
        # Price left a gap on the way down — bearish FVG.
        return FairValueGap(
            direction=DisplacementDirection.BEARISH,
            gap_high=candle_before.low,
            gap_low=candle_after.high,
            formed_at_candle_open_time=displacement_candle.open_time.isoformat(),
        )
    if candle_before.high < candle_after.low:
        # Price left a gap on the way up — bullish FVG.
        return FairValueGap(
            direction=DisplacementDirection.BULLISH,
            gap_high=candle_after.low,
            gap_low=candle_before.high,
            formed_at_candle_open_time=displacement_candle.open_time.isoformat(),
        )
    return None
