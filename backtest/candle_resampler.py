"""
backtest/candle_resampler.py

Neither Fyers nor AngelOne offer a native 75-minute candle resolution
(confirmed: Fyers' documented resolutions are 1/2/3/5/10/15/20/30/60/
120/240/D; AngelOne's are ONE_MINUTE through THIRTY_MINUTE plus ONE_HOUR/
ONE_DAY) — so this backtest always fetches the finest available
resolution (1-minute) and resamples locally to whatever timeframes
strategy needs (3m/5m LTF, 75m HTF per config/settings.yaml).

Bucketing uses the exact same midnight-IST-anchored rule as
data_feed/candle_aggregator.py's live path, so a backtested 75m candle
and a live 75m candle for the same time window are identically defined —
that consistency is the whole point of reusing one bucketing rule rather
than inventing a second one for backtests.
"""
from __future__ import annotations

from datetime import datetime, timezone

from data_feed.candle_aggregator import Candle, _bucket_start


def resample_candles(
    raw_candles: list[tuple[float, float, float, float, float, float]],
    instrument_key: str,
    target_minutes: int,
) -> list[Candle]:
    """raw_candles: chronologically ordered (epoch_seconds, open, high,
    low, close, volume) tuples, typically 1-minute bars from a broker's
    historical API. Returns closed Candle objects at target_minutes
    resolution — the last bucket is included even if not "closed" by a
    later bar, since backtests replay a fixed historical range rather
    than a live stream."""
    if not raw_candles:
        return []

    buckets: dict[datetime, Candle] = {}
    order: list[datetime] = []

    for epoch, o, h, l, c, _v in raw_candles:
        ts = datetime.fromtimestamp(epoch, tz=timezone.utc)
        bucket_start = _bucket_start(ts, target_minutes)

        if bucket_start not in buckets:
            buckets[bucket_start] = Candle(
                instrument_key=instrument_key, timeframe_minutes=target_minutes,
                open_time=bucket_start, open=o, high=h, low=l, close=c, tick_count=1,
            )
            order.append(bucket_start)
        else:
            candle = buckets[bucket_start]
            candle.high = max(candle.high, h)
            candle.low = min(candle.low, l)
            candle.close = c
            candle.tick_count += 1

    return [buckets[ts] for ts in order]
