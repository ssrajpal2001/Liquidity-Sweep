"""
data_feed/protobuf_decoder.py

The official `upstox-python` SDK's MarketDataStreamerV3 already does the
actual Protobuf decoding internally (FeedResponse.FromString + MessageToDict)
and hands us a plain dict. Verified against the installed SDK
(upstox_client.feeder.market_data_streamer_v3.MarketDataStreamerV3.handle_message):

    def handle_message(self, ws, message):
        decoded_data = self.decode_protobuf(message)
        data_dict = json_format.MessageToDict(decoded_data)
        self.emit(self.Event["MESSAGE"], data_dict)

So this module's job is NOT re-decoding Protobuf — it's normalizing that
already-decoded dict (deeply nested, and its exact shape can vary by
subscription mode: ltpc / full / option_greeks / full_d30) into one flat,
stable internal `Tick` type the rest of the bot depends on. This isolates
every other module from Upstox's wire format.

IMPORTANT: the exact nesting under feeds -> {instrument_key} -> ... has not
been verified against a live message in this environment (no network path
to api.upstox.com from here). normalize_message() is deliberately
defensive: unrecognized shapes are logged and skipped, never raised, so a
schema surprise during tomorrow's sandbox run shows up clearly in
logs/bot.log as a WARNING with the raw dict — please paste that back to me
if you see any "Unrecognized feed shape" warnings.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class Tick:
    instrument_key: str
    ltp: float
    ltt_epoch_ms: Optional[int]   # last trade time, epoch millis
    ltq: Optional[int]            # last trade quantity
    close_prev_day: Optional[float]
    raw: dict[str, Any]           # original per-instrument dict, kept for debugging


def _first_present(d: dict, *keys: str) -> Optional[dict]:
    for k in keys:
        if k in d:
            return d[k]
    return None


def _extract_ltpc(instrument_feed: dict) -> Optional[dict]:
    """Feed shape varies by subscription mode. Known/likely wrapper keys,
    checked in order, based on Upstox's documented mode names (ltpFeed,
    fullFeed) and community sample payloads (marketFF -> ltpc)."""
    feed_body = _first_present(instrument_feed, "ltpFeed", "fullFeed", "firstLevelWithGreeks")
    if feed_body is None:
        return None
    market_data = _first_present(feed_body, "marketFF", "ltpc", "ltpC")
    if market_data is None:
        return None
    ltpc = market_data.get("ltpc") if isinstance(market_data, dict) and "ltpc" in market_data else market_data
    return ltpc if isinstance(ltpc, dict) and "ltp" in ltpc else None


def normalize_message(data_dict: dict[str, Any]) -> list[Tick]:
    """Takes one already-decoded SDK message dict and returns zero or more
    Ticks (a single message can carry updates for many subscribed
    instruments at once)."""
    ticks: list[Tick] = []

    feeds = data_dict.get("feeds")
    if not feeds:
        # Market-status / heartbeat messages have no "feeds" key — not an error.
        logger.debug("Message with no feeds (likely market-status/heartbeat): %s", data_dict.get("type"))
        return ticks

    for instrument_key, instrument_feed in feeds.items():
        ltpc = _extract_ltpc(instrument_feed)
        if ltpc is None:
            logger.warning(
                "Unrecognized feed shape for %s — please share this line from "
                "logs/bot.log so the decoder can be adjusted: %s",
                instrument_key, instrument_feed,
            )
            continue

        try:
            ltp = float(ltpc["ltp"])
        except (KeyError, TypeError, ValueError):
            logger.warning("Feed for %s missing/invalid ltp: %s", instrument_key, ltpc)
            continue

        ltt_raw = ltpc.get("ltt")
        ltq_raw = ltpc.get("ltq")
        cp_raw = ltpc.get("cp")

        ticks.append(
            Tick(
                instrument_key=instrument_key,
                ltp=ltp,
                ltt_epoch_ms=int(ltt_raw) if ltt_raw not in (None, "") else None,
                ltq=int(ltq_raw) if ltq_raw not in (None, "") else None,
                close_prev_day=float(cp_raw) if cp_raw not in (None, "") else None,
                raw=instrument_feed,
            )
        )

    return ticks
