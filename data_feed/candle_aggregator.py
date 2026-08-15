"""
data_feed/candle_aggregator.py

Aggregates a stream of Ticks (from protobuf_decoder.normalize_message) into
OHLC candles for however many timeframes the strategy needs — in
particular the 3-min/5-min (LTF), 15-min (ITF), and 75-min (HTF) stack
config/settings.yaml declares under `timeframes:`.

This module has no dependency on any specific broker at all — it takes (instrument_key,
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


SESSION_START_MINUTES = 9 * 60 + 15  # 09:15 IST — NSE/BSE market open


def _bucket_start(ts: datetime, minutes: int) -> datetime:
    """Floors a timestamp to the start of its N-minute bucket, anchored
    to market open (09:15 IST), not midnight.

    This matters specifically for 75-minute HTF candles: 09:15 is NOT a
    multiple of 75 minutes since midnight (555 minutes), so a naive
    midnight-anchored bucketing produces 08:45-10:00, 10:00-11:15, ...
    instead of the conventional 09:15-10:30, 10:30-11:45, ... bars every
    trading platform actually shows. This was caught while building the
    backtest (a 75-candle window landed in 2 buckets instead of 1) and
    matters for live trading too, since strategy/rolling_base.py's HTF
    levels are computed from these same candles.

    Anchoring to session open is mathematically a no-op for 3/5/15-minute
    buckets — 555 (minutes since midnight at 09:15) divides evenly by
    3, 5, and 15, so those timeframes bucket identically either way. Only
    75-minute (and any other non-divisor of 555) actually changes.
    """
    ts = ts.astimezone(IST)
    midnight = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes_since_midnight = (ts - midnight).total_seconds() / 60
    offset = minutes_since_midnight - SESSION_START_MINUTES
    bucket_index = offset // minutes
    bucket_start_minutes = SESSION_START_MINUTES + bucket_index * minutes
    # timedelta arithmetic (not ts.replace(hour=..., minute=...)) so a
    # bucket that starts before midnight or past 24:00 rolls the date
    # correctly instead of raising ValueError('hour must be in 0..23') —
    # live ticks only ever arrive during market hours so this never
    # mattered there, but backtest/off-hours data can easily hit it.
    return midnight + timedelta(minutes=bucket_start_minutes)


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

    def bootstrap_instrument(
        self, instrument_key: str, raw_1min_candles: list[tuple[float, float, float, float, float, float]]
    ) -> None:
        """Resamples raw (epoch, o, h, l, c, v) 1-minute candles to every
        registered timeframe for this instrument and seeds each
        TimeframeAggregator from them — used on startup and, critically,
        on WS reconnect: a gap in ticks would otherwise leave whatever
        candle was in progress silently truncated instead of correctly
        completed from the missed history."""
        from data_feed.candle_resampler import resample_candles

        for (inst_key, timeframe_minutes), agg in self._aggregators.items():
            if inst_key != instrument_key:
                continue
            resampled = resample_candles(raw_1min_candles, instrument_key, timeframe_minutes)
            agg.bootstrap_from_historical(resampled)
