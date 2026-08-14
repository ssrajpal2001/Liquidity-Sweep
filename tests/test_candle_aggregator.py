from __future__ import annotations

from datetime import datetime, timezone

from data_feed.candle_aggregator import Candle, TimeframeAggregator


def _ts(hour, minute, second=0):
    return datetime(2026, 8, 14, hour, minute, second, tzinfo=timezone.utc) \
        .astimezone(timezone.utc)  # UTC in, aggregator converts to IST internally


def test_ticks_within_same_bucket_update_one_candle():
    closed = []
    agg = TimeframeAggregator("NIFTY", 3, on_close=closed.append)

    # 09:15:00 IST == 03:45:00 UTC
    base = datetime(2026, 8, 14, 3, 45, 0, tzinfo=timezone.utc)
    agg.ingest(100.0, base)
    agg.ingest(105.0, base.replace(second=30))
    agg.ingest(98.0, base.replace(minute=46, second=59))

    assert closed == []  # still same 3-min bucket
    assert agg._current.open == 100.0
    assert agg._current.high == 105.0
    assert agg._current.low == 98.0
    assert agg._current.close == 98.0
    assert agg._current.tick_count == 3


def test_tick_in_next_bucket_closes_previous_candle():
    closed = []
    agg = TimeframeAggregator("NIFTY", 3, on_close=closed.append)

    base = datetime(2026, 8, 14, 3, 45, 0, tzinfo=timezone.utc)  # 09:15 IST
    agg.ingest(100.0, base)
    agg.ingest(110.0, base.replace(second=30))

    next_bucket = datetime(2026, 8, 14, 3, 48, 5, tzinfo=timezone.utc)  # 09:18 IST
    agg.ingest(112.0, next_bucket)

    assert len(closed) == 1
    assert closed[0].open == 100.0
    assert closed[0].high == 110.0
    assert closed[0].close == 110.0
    assert agg._current.open == 112.0  # new bucket seeded correctly


def test_late_tick_does_not_corrupt_current_candle():
    closed = []
    agg = TimeframeAggregator("NIFTY", 3, on_close=closed.append)

    base = datetime(2026, 8, 14, 3, 48, 0, tzinfo=timezone.utc)  # 09:18 IST
    agg.ingest(100.0, base)

    late = datetime(2026, 8, 14, 3, 45, 0, tzinfo=timezone.utc)  # 09:15 IST (earlier bucket)
    agg.ingest(999.0, late)

    assert agg._current.high == 100.0  # late tick ignored, didn't corrupt current candle
    assert closed == []
