"""
data_feed/upstox_ws_client.py

Wraps the official SDK's `upstox_client.MarketDataStreamerV3`, which
already: opens the v3 market-feed WebSocket, decodes Protobuf messages into
dicts, and retries closed connections a limited number of times at a fixed
1-second interval (verified against the installed SDK: `enable_auto_reconnect
= True`, `interval = 1`, `retry_count = 5` in `feeder.streamer.Streamer`).

That built-in retry is NOT exponential backoff and gives up after 5 tries
(emitting `autoReconnectStopped`). This wrapper adds the two things the
architecture plan requires on top of it:
  1. An outer exponential-backoff loop that takes over once the SDK's own
     retries are exhausted, instead of leaving the feed dead.
  2. A REST-resync hook fired on every successful (re)connect, so a gap in
     ticks doesn't silently produce a truncated candle — the caller passes
     a resync callback that goes out to Historical Candle V3 for whatever
     candles may have been missed.

`connect()` is non-blocking — the SDK spawns its own background thread
running `websocket.WebSocketApp.run_forever()`. Callbacks below therefore
run on that thread; `on_tick` must be safe to call from it (the candle
aggregator this feeds is plain synchronous Python, so it is).
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable, Optional

import upstox_client

from data_feed.protobuf_decoder import Tick, normalize_message

logger = logging.getLogger(__name__)

OuterReconnectCallback = Callable[[], None]
TickCallback = Callable[[Tick], None]

MAX_BACKOFF_SECONDS = 60
BASE_BACKOFF_SECONDS = 2


class UpstoxWSClient:
    def __init__(
        self,
        api_client: "upstox_client.ApiClient",
        instrument_keys: list[str],
        on_tick: TickCallback,
        on_reconnect: Optional[OuterReconnectCallback] = None,
        mode: str = "full",
    ):
        self.api_client = api_client
        self.instrument_keys = instrument_keys
        self.on_tick = on_tick
        self.on_reconnect = on_reconnect
        self.mode = mode

        self.streamer: Optional["upstox_client.MarketDataStreamerV3"] = None
        self._connected_once = False
        self._is_open = False
        self._outer_reconnect_attempt = 0
        self._stopping = threading.Event()
        self._last_message_at: Optional[float] = None
        self._dynamic_subscriptions: dict[str, str] = {}  # instrument_key -> mode, survives reconnects

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        self._stopping.clear()
        self._build_streamer()
        self.streamer.connect()  # non-blocking; runs on a background thread

    def stop(self) -> None:
        self._stopping.set()
        if self.streamer is not None:
            try:
                self.streamer.disconnect()
            except Exception:  # noqa: BLE001
                logger.exception("Error while disconnecting WS streamer.")

    def _build_streamer(self) -> None:
        streamer = upstox_client.MarketDataStreamerV3(
            self.api_client, self.instrument_keys, self.mode
        )
        streamer.on(streamer.Event["OPEN"], self._handle_open)
        streamer.on(streamer.Event["MESSAGE"], self._handle_message)
        streamer.on(streamer.Event["ERROR"], self._handle_error)
        streamer.on(streamer.Event["CLOSE"], self._handle_close)
        streamer.on(streamer.Event["RECONNECTING"], self._handle_sdk_reconnecting)
        streamer.on(streamer.Event["AUTO_RECONNECT_STOPPED"], self._handle_auto_reconnect_stopped)
        self.streamer = streamer

    # -- SDK event handlers ------------------------------------------------------
    def _handle_open(self, *_args) -> None:
        was_reconnect = self._connected_once
        self._connected_once = True
        self._is_open = True
        self._outer_reconnect_attempt = 0
        logger.info("Upstox WS connected (instruments=%d, mode=%s).",
                     len(self.instrument_keys), self.mode)
        if was_reconnect:
            if self._dynamic_subscriptions:
                logger.info(
                    "Reconnected — re-applying %d dynamic subscription(s) lost on disconnect.",
                    len(self._dynamic_subscriptions),
                )
                for instrument_key, mode in self._dynamic_subscriptions.items():
                    self.subscribe([instrument_key], mode)
            if self.on_reconnect is not None:
                logger.info("Reconnected — invoking REST resync for any missed candles.")
                try:
                    self.on_reconnect()
                except Exception:  # noqa: BLE001
                    logger.exception("REST resync callback failed after reconnect.")

    def _handle_message(self, data_dict: dict) -> None:
        self._last_message_at = time.time()
        try:
            ticks = normalize_message(data_dict)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to normalize WS message: %s", data_dict)
            return
        for tick in ticks:
            try:
                self.on_tick(tick)
            except Exception:  # noqa: BLE001
                logger.exception("on_tick callback raised for %s", tick.instrument_key)

    def _handle_error(self, *args) -> None:
        logger.error("Upstox WS error: %s", args)

    def _handle_close(self, *args) -> None:
        self._is_open = False
        logger.warning("Upstox WS closed: %s", args)

    def _handle_sdk_reconnecting(self, message: str) -> None:
        logger.warning("Upstox WS SDK-level reconnect: %s", message)

    def _handle_auto_reconnect_stopped(self, *args) -> None:
        logger.error(
            "Upstox WS SDK exhausted its built-in reconnect attempts. "
            "Switching to outer exponential-backoff reconnect."
        )
        self._outer_reconnect_loop()

    # -- outer exponential backoff, on top of the SDK's own limited retry -------
    def _outer_reconnect_loop(self) -> None:
        def run():
            while not self._stopping.is_set():
                self._outer_reconnect_attempt += 1
                delay = min(
                    MAX_BACKOFF_SECONDS,
                    BASE_BACKOFF_SECONDS * (2 ** (self._outer_reconnect_attempt - 1)),
                )
                delay += random.uniform(0, 1)  # jitter, avoid thundering herd on shared infra
                logger.warning(
                    "Outer reconnect attempt %d in %.1fs...",
                    self._outer_reconnect_attempt, delay,
                )
                time.sleep(delay)
                if self._stopping.is_set():
                    return
                try:
                    self._build_streamer()
                    self.streamer.connect()
                    return  # _handle_open fires on success and resets attempt count
                except Exception:  # noqa: BLE001
                    logger.exception("Outer reconnect attempt %d failed.",
                                      self._outer_reconnect_attempt)
                    continue

        threading.Thread(target=run, daemon=True, name="ws-outer-reconnect").start()

    # -- health check --------------------------------------------------------
    def seconds_since_last_message(self) -> Optional[float]:
        if self._last_message_at is None:
            return None
        return time.time() - self._last_message_at

    # -- dynamic subscriptions (e.g. subscribing to an option leg only once a
    #    position actually opens, rather than subscribing to the whole chain
    #    up front) --------------------------------------------------------
    def subscribe(self, instrument_keys: list[str], mode: str = "ltpc") -> bool:
        """Adds instrument_keys to the live subscription. Returns False
        (and logs, doesn't raise) if the socket isn't open yet — callers
        in this bot only ever call this right after a synchronously
        confirmed order fill, by which point the feed has been open for
        the whole signal-detection pipeline, but the guard is here in case
        that assumption is ever violated (e.g. a future async refactor)."""
        if self.streamer is None or not self._is_open:
            logger.error(
                "Cannot subscribe to %s — WS not open yet.", instrument_keys
            )
            return False
        try:
            self.streamer.subscribe(instrument_keys, mode)
            for key in instrument_keys:
                self._dynamic_subscriptions[key] = mode
            logger.info("Subscribed to %s (mode=%s).", instrument_keys, mode)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("Failed to subscribe to %s", instrument_keys)
            return False

    def unsubscribe(self, instrument_keys: list[str]) -> None:
        if self.streamer is None or not self._is_open:
            return
        try:
            self.streamer.unsubscribe(instrument_keys)
            for key in instrument_keys:
                self._dynamic_subscriptions.pop(key, None)
            logger.info("Unsubscribed from %s.", instrument_keys)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to unsubscribe from %s", instrument_keys)
