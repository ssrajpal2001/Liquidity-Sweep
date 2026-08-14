"""
data_feed/candle_aggregator.py

Aggregates a stream of Ticks (from protobuf_decoder.normalize_message) into
OHLC candles for however many timeframes the strategy needs — in
particular the 3-min/5-min (LTF), 15-min (ITF), and 75-min (HTF) stack
config/settings.yaml declares under `timeframes:`.

This module has no dependency on Upstox at all — it takes (instrument_key,
price, epoch_ms) in and emits closed Candle objects out — so it's fully
unit-testable with synthetic ticks, independent of the WS client.

Bucketing rule: a tick at time T belongs to the candle whose window is
[floor(T, interval), floor(T, interval) + interval). A candle is emitted as
"closed" the moment a tick arrives for the NEXT window — i.e. on the first
tick past the boundary, not on a wall-clock timer. This matches how
exchange candles are conventionally defined and avoids needing a separate
scheduler thread just to close candles on time.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class Candle:
    instrument_key: str
    timeframe_minutes: int
    open_time: datetime   # IST, start of the bucket
    open: float
    high: float
    low: float
    close: float
    tick_count: int = 0

    def update(self, price: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.tick_count += 1


CandleCallback = Callable[[Candle], None]


def _bucket_start(ts: datetime, minutes: int) -> datetime:
    """Floors a timestamp to the start of its N-minute bucket, anchored to
    midnight IST (i.e. buckets are 00:00, 00:0N, 00:2N, ... not anchored to
    market open) — this matches how exchange-provided N-minute candles are
    conventionally bucketed."""
    ts = ts.astimezone(IST)
    minutes_since_midnight = ts.hour * 60 + ts.minute
    bucket_index = minutes_since_midnight // minutes
    bucket_start_minutes = bucket_index * minutes
    return ts.replace(
        hour=bucket_start_minutes // 60,
        minute=bucket_start_minutes % 60,
        second=0,
        microsecond=0,
    )


class TimeframeAggregator:
    """Aggregates ticks for ONE (instrument, timeframe) pair."""

    def __init__(self, instrument_key: str, timeframe_minutes: int, on_close: CandleCallback):
        self.instrument_key = instrument_key
        self.timeframe_minutes = timeframe_minutes
        self.on_close = on_close
        self._current: Optional[Candle] = None

    def ingest(self, price: float, ts: datetime) -> None:
        bucket_start = _bucket_start(ts, self.timeframe_minutes)

        if self._current is None:
            self._current = Candle(
                instrument_key=self.instrument_key,
                timeframe_minutes=self.timeframe_minutes,
                open_time=bucket_start,
                open=price, high=price, low=price, close=price,
            )
            self._current.update(price)
            self._current.tick_count = 1
            return

        if bucket_start == self._current.open_time:
            self._current.update(price)
            return

        if bucket_start > self._current.open_time:
            closed = self._current
            self.on_close(closed)
            self._current = Candle(
                instrument_key=self.instrument_key,
                timeframe_minutes=self.timeframe_minutes,
                open_time=bucket_start,
                open=price, high=price, low=price, close=price,
                tick_count=1,
            )
            return

        # bucket_start < current.open_time: an out-of-order/late tick
        # (can happen after a WS reconnect race). Don't corrupt the live
        # candle; just log it — the REST resync path is what repairs gaps.
        logger.warning(
            "Late tick for %s ignored (tick bucket %s < current candle %s).",
            self.instrument_key, bucket_start, self._current.open_time,
        )

    def bootstrap_from_historical(self, candles: list[Candle]) -> None:
        """Seeds this aggregator's in-progress candle from historical REST
        data at startup/reconnect, so the first live tick doesn't open a
        candle with a truncated history behind it."""
        if not candles:
            return
        self._current = candles[-1]
        for c in candles[:-1]:
            self.on_close(c)


class CandleAggregator:
    """Owns one TimeframeAggregator per (instrument, timeframe) combination."""

    def __init__(self, on_close: CandleCallback):
        self.on_close = on_close
        self._aggregators: dict[tuple[str, int], TimeframeAggregator] = {}

    def register(self, instrument_key: str, timeframe_minutes: int) -> None:
        key = (instrument_key, timeframe_minutes)
        if key not in self._aggregators:
            self._aggregators[key] = TimeframeAggregator(
                instrument_key, timeframe_minutes, self.on_close
            )

    def ingest_tick(self, instrument_key: str, price: float, epoch_ms: Optional[int]) -> None:
        ts = (
            datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
            if epoch_ms is not None
            else datetime.now(timezone.utc)
        )
        for (inst_key, _tf), agg in self._aggregators.items():
            if inst_key == instrument_key:
                agg.ingest(price, ts)
