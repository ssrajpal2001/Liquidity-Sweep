from __future__ import annotations

from datetime import datetime, timezone

from data_feed.candle_aggregator import Candle
from strategy.displacement import detect_displacement, detect_fvg
from strategy.sweep_detector import SweepDirection, detect_sweep


def _candle(o, h, l, c, minute=0):
    return Candle(
        instrument_key="NIFTY", timeframe_minutes=3,
        open_time=datetime(2026, 8, 14, 9, minute, tzinfo=timezone.utc),
        open=o, high=h, low=l, close=c,
    )


# -- sweep detector -----------------------------------------------------------

def test_bearish_sweep_pierce_and_close_back_inside():
    # base high = 100. Candle pierces to 102 but closes back at 99 -> real sweep.
    candle = _candle(o=99.5, h=102.0, l=99.0, c=99.0)
    event = detect_sweep(candle, level_high=100.0, level_low=90.0)
    assert event is not None
    assert event.direction == SweepDirection.BEARISH
    assert event.pierce_points == 2.0


def test_no_sweep_full_body_close_beyond_level_is_continuation_not_sweep():
    # Closes at 103, beyond the level -> false sweep / continuation, not a real sweep.
    candle = _candle(o=100.5, h=103.5, l=100.0, c=103.0)
    event = detect_sweep(candle, level_high=100.0, level_low=90.0)
    assert event is None


def test_bullish_sweep_pierce_and_close_back_inside():
    candle = _candle(o=91.0, h=91.5, l=88.0, c=91.0)
    event = detect_sweep(candle, level_high=100.0, level_low=90.0)
    assert event is not None
    assert event.direction == SweepDirection.BULLISH
    assert event.pierce_points == 2.0


def test_no_pierce_no_sweep():
    candle = _candle(o=95.0, h=97.0, l=93.0, c=96.0)
    event = detect_sweep(candle, level_high=100.0, level_low=90.0)
    assert event is None


# -- displacement ---------------------------------------------------------

def test_displacement_confirmed_on_large_bearish_body_breaking_swing_low():
    lookback = [_candle(100, 101, 99, 100, minute=i) for i in range(5)]  # ATR = 2.0
    # Body size = |92 - 100| = 8, 8/2.0 = 4x ATR > 1.5x threshold. Bearish body, breaks swing low of 98.
    candidate = _candle(o=100, h=100.5, l=91.5, c=92, minute=6)
    event = detect_displacement(
        candidate, lookback, micro_swing_high=101, micro_swing_low=98, atr_multiplier=1.5
    )
    assert event is not None
    assert event.direction.value == "bearish"


def test_no_displacement_when_body_too_small_relative_to_atr():
    lookback = [_candle(100, 105, 95, 100, minute=i) for i in range(5)]  # ATR = 10
    candidate = _candle(o=100, h=101, l=99, c=100.5, minute=6)  # tiny body
    event = detect_displacement(candidate, lookback, micro_swing_high=105, micro_swing_low=95)
    assert event is None


def test_fvg_detected_for_bearish_gap():
    before = _candle(101, 102, 99, 100, minute=0)   # low = 99
    disp = _candle(98, 98.5, 90, 91, minute=3)
    after = _candle(90, 92, 89, 91, minute=6)        # high = 92 < before.low (99) -> gap
    fvg = detect_fvg(before, disp, after)
    assert fvg is not None
    assert fvg.direction.value == "bearish"
    assert fvg.gap_high == 99
    assert fvg.gap_low == 92


def test_no_fvg_when_candles_overlap():
    before = _candle(100, 102, 98, 100, minute=0)
    disp = _candle(99, 100, 95, 96, minute=3)
    after = _candle(96, 99, 94, 97, minute=6)  # overlaps with 'before' range -> no gap
    fvg = detect_fvg(before, disp, after)
    assert fvg is None
