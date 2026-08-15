from __future__ import annotations

from datetime import datetime, timezone

from data_feed.candle_resampler import resample_candles


def _minute_candle(base_epoch: float, minute_offset: int, price: float):
    epoch = base_epoch + minute_offset * 60
    return (epoch, price, price + 2, price - 2, price + 1, 1000)


def test_resamples_1min_bars_into_correct_number_of_3min_buckets():
    base = datetime(2026, 8, 10, 3, 45, tzinfo=timezone.utc).timestamp()  # 09:15 IST
    raw = [_minute_candle(base, i, 25000 + i) for i in range(6)]
    result = resample_candles(raw, "NIFTY", target_minutes=3)
    assert len(result) == 2
    assert result[0].tick_count == 3
    assert result[1].tick_count == 3


def test_ohlc_aggregation_is_correct():
    base = datetime(2026, 8, 10, 3, 45, tzinfo=timezone.utc).timestamp()
    raw = [
        (base, 100, 105, 98, 102, 500),
        (base + 60, 102, 110, 101, 108, 500),
        (base + 120, 108, 109, 95, 96, 500),
    ]
    result = resample_candles(raw, "NIFTY", target_minutes=3)
    assert len(result) == 1
    c = result[0]
    assert c.open == 100    # first candle's open
    assert c.close == 96    # last candle's close
    assert c.high == 110    # max across all three
    assert c.low == 95      # min across all three


def test_resamples_into_75min_htf_correctly():
    base = datetime(2026, 8, 10, 3, 45, tzinfo=timezone.utc).timestamp()  # 09:15 IST — market open
    raw = [_minute_candle(base, i, 25000) for i in range(75)]  # exactly one session-aligned 75m bucket
    result = resample_candles(raw, "NIFTY", target_minutes=75)
    assert len(result) == 1
    assert result[0].tick_count == 75
    # Regression test for the anchoring bug this exact test caught: 75-min
    # buckets must align to market open (09:15), not midnight — a naive
    # midnight anchor would split this into 2 buckets (08:45-10:00 boundary).
    from datetime import timezone as tz, timedelta
    IST = tz(timedelta(hours=5, minutes=30))
    assert result[0].open_time == datetime(2026, 8, 10, 9, 15, tzinfo=IST)


def test_empty_input_returns_empty_list():
    assert resample_candles([], "NIFTY", target_minutes=3) == []


def test_candles_are_in_chronological_order():
    base = datetime(2026, 8, 10, 3, 45, tzinfo=timezone.utc).timestamp()
    raw = [_minute_candle(base, i, 25000 + i) for i in range(30)]
    result = resample_candles(raw, "NIFTY", target_minutes=5)
    for i in range(len(result) - 1):
        assert result[i].open_time < result[i + 1].open_time


def test_resampling_off_hours_data_does_not_crash():
    """Regression test for a real bug: timestamps outside market hours
    (or, as here, historical data that runs past midnight) used to raise
    ValueError('hour must be in 0..23') because _bucket_start built the
    bucket boundary with ts.replace(hour=..., minute=...), which can't
    represent day rollover. Fixed with timedelta arithmetic instead. Live
    ticks never hit this (market hours only), but backtests can."""
    base = datetime(2026, 8, 10, 3, 45, tzinfo=timezone.utc).timestamp()  # 09:15 IST
    # 600 minutes runs well past market close and across midnight.
    raw = [_minute_candle(base, i, 25000) for i in range(600)]
    result = resample_candles(raw, "NIFTY", target_minutes=75)  # must not raise
    assert len(result) > 0
