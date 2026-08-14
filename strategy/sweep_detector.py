"""
strategy/sweep_detector.py

LTF (3m/5m) sweep detection against a given HTF level (typically the
current RollingBase.high/low from strategy/rolling_base.py, but works
against any level — also used for PDH/PDL and equal-highs/lows checks).

Rule (from the architecture): a sweep event fires when
    bearish: candle.high > level AND candle.close < level
    bullish: candle.low  < level AND candle.close > level
i.e. price pierces the level intrabar but the candle's own close rejects
back inside it — a full-body close beyond the level is NOT a sweep, it's
the false-sweep/continuation case handled by requiring this exact
pierce-then-reject shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from data_feed.candle_aggregator import Candle


class SweepDirection(str, Enum):
    BEARISH = "bearish"  # swept a high -> potential PUT setup
    BULLISH = "bullish"  # swept a low  -> potential CALL setup


@dataclass
class SweepEvent:
    direction: SweepDirection
    level: float
    candle: Candle
    pierce_points: float  # how far beyond the level price went before rejecting


def detect_sweep(candle: Candle, level_high: float, level_low: float) -> SweepEvent | None:
    """Checks one closed LTF candle against a two-sided level (e.g. a
    rolling base's high AND low) and returns at most one SweepEvent —
    a single candle cannot validly sweep both sides."""
    if candle.high > level_high and candle.close < level_high:
        return SweepEvent(
            direction=SweepDirection.BEARISH,
            level=level_high,
            candle=candle,
            pierce_points=candle.high - level_high,
        )
    if candle.low < level_low and candle.close > level_low:
        return SweepEvent(
            direction=SweepDirection.BULLISH,
            level=level_low,
            candle=candle,
            pierce_points=level_low - candle.low,
        )
    return None
