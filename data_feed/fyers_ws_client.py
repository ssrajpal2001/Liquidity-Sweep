"""
data_feed/fyers_ws_client.py

Wraps the official SDK's `fyers_apiv3.FyersWebsocket.data_ws.FyersDataSocket`.
Verified against the installed SDK:
  - connect() spawns the actual WebSocketApp.run_forever() on its own
    background thread (self.ws_thread) — non-blocking, same pattern as
    the Upstox SDK had.
  - Messages arrive as plain JSON dicts (no Protobuf) with at least
    'symbol' and 'ltp' keys for SymbolUpdate mode — confirmed against
    real sample code, not just docs.
  - Built-in reconnect (reconnect=True, reconnect_retry=5) exists but is
    NOT exponential and gives up after a fixed number of tries, same
    limitation as Upstox's SDK — so this wrapper adds the same outer
    exponential-backoff loop on top, and the same "REST resync on
    reconnect" hook, so the architecture's Phase 2 requirements (Edge
    Cases 1 and 3 from the review) are met regardless of which broker
    is underneath.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable, Optional

from fyers_apiv3.FyersWebsocket import data_ws

logger = logging.getLogger(__name__)

TickCallback = Callable[[str, float], None]  # (symbol, ltp)
ReconnectCallback = Callable[[], None]

MAX_BACKOFF_SECONDS = 60
BASE_BACKOFF_SECONDS = 2


class FyersWSClient:
    def __init__(
        self,
        combined_token: str,          # "client_id:access_token", from FyersAuth.get_valid_token()
        symbols: list[str],
        on_tick: TickCallback,
        on_reconnect: Optional[ReconnectCallback] = None,
        data_type: str = "SymbolUpdate",
        litemode: bool = True,        # LTP-only is all the candle/position pipeline needs
    ):
        self.combined_token = combined_token
        self.symbols = symbols
        self.on_tick = on_tick
        self.on_reconnect = on_reconnect
        self.data_type = data_type
        self.litemode = litemode

        self.socket: Optional["data_ws.FyersDataSocket"] = None
        self._connected_once = False
        self._is_open = False
        self._outer_reconnect_attempt = 0
        self._stopping = threading.Event()
        self._last_message_at: Optional[float] = None
        self._dynamic_symbols: dict[str, str] = {}  # symbol -> data_type, survives reconnects

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        self._stopping.clear()
        self._build_socket()
        self.socket.connect()  # non-blocking; runs on a background thread

    def stop(self) -> None:
        self._stopping.set()
        if self.socket is not None:
            try:
                self.socket.close_connection()
            except Exception:  # noqa: BLE001
                logger.exception("Error while closing WS socket.")

    def _build_socket(self) -> None:
        self.socket = data_ws.FyersDataSocket(
            access_token=self.combined_token,
            write_to_file=False,
            log_path="",
            litemode=self.litemode,
            reconnect=True,
            on_connect=self._handle_open,
            on_close=self._handle_close,
            on_error=self._handle_error,
            on_message=self._handle_message,
        )

    # -- SDK event handlers ------------------------------------------------------
    def _handle_open(self) -> None:
        was_reconnect = self._connected_once
        self._connected_once = True
        self._is_open = True
        self._outer_reconnect_attempt = 0
        logger.info("Fyers WS connected (symbols=%d).", len(self.symbols))

        try:
            self.socket.subscribe(symbols=self.symbols, data_type=self.data_type)
            logger.info("Subscribed to initial symbols: %s", self.symbols)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to subscribe to initial symbols on open.")

        if was_reconnect:
            if self._dynamic_symbols:
                logger.info(
                    "Reconnected — re-applying %d dynamic subscription(s) lost on disconnect.",
                    len(self._dynamic_symbols),
                )
                for symbol, dtype in self._dynamic_symbols.items():
                    self.subscribe([symbol], dtype)
            if self.on_reconnect is not None:
                logger.info("Reconnected — invoking REST resync for any missed candles.")
                try:
                    self.on_reconnect()
                except Exception:  # noqa: BLE001
                    logger.exception("REST resync callback failed after reconnect.")

    def _handle_message(self, message: dict) -> None:
        self._last_message_at = time.time()
        symbol = message.get("symbol")
        ltp = message.get("ltp")
        if symbol is None or ltp is None:
            logger.warning(
                "Unrecognized tick shape (missing symbol/ltp) — please share this "
                "line from logs/bot.log so the handler can be adjusted: %s", message,
            )
            return
        try:
            self.on_tick(symbol, float(ltp))
        except Exception:  # noqa: BLE001
            logger.exception("on_tick callback raised for %s", symbol)

    def _handle_error(self, message) -> None:
        logger.error("Fyers WS error: %s", message)

    def _handle_close(self, message) -> None:
        self._is_open = False
        logger.warning("Fyers WS closed: %s", message)
        # The SDK's own reconnect (reconnect=True, retry=5) will attempt
        # first; if it exhausts its retries the socket simply stays closed
        # with no further event — so we watch for that via the health-check
        # loop in main.py (seconds_since_last_message) rather than a
        # dedicated "gave up" callback, which this SDK doesn't expose.

    # -- outer exponential backoff, layered on top of the SDK's own limited retry -----
    def force_outer_reconnect(self) -> None:
        """Call this from main.py's health-check loop if the connection has
        been down long enough that the SDK's own fixed-retry reconnect has
        plausibly exhausted itself."""
        if self._is_open or self._stopping.is_set():
            return

        def run():
            while not self._stopping.is_set() and not self._is_open:
                self._outer_reconnect_attempt += 1
                delay = min(
                    MAX_BACKOFF_SECONDS,
                    BASE_BACKOFF_SECONDS * (2 ** (self._outer_reconnect_attempt - 1)),
                )
                delay += random.uniform(0, 1)
                logger.warning("Outer reconnect attempt %d in %.1fs...",
                                self._outer_reconnect_attempt, delay)
                time.sleep(delay)
                if self._stopping.is_set() or self._is_open:
                    return
                try:
                    self._build_socket()
                    self.socket.connect()
                    return  # _handle_open fires on success and resets attempt count
                except Exception:  # noqa: BLE001
                    logger.exception("Outer reconnect attempt %d failed.",
                                      self._outer_reconnect_attempt)
                    continue

        threading.Thread(target=run, daemon=True, name="ws-outer-reconnect").start()

    # -- dynamic subscriptions (e.g. an option leg once a position opens) --------
    def subscribe(self, symbols: list[str], data_type: str = "SymbolUpdate") -> bool:
        if self.socket is None or not self._is_open:
            logger.error("Cannot subscribe to %s — WS not open yet.", symbols)
            return False
        try:
            self.socket.subscribe(symbols=symbols, data_type=data_type)
            for s in symbols:
                self._dynamic_symbols[s] = data_type
            logger.info("Subscribed to %s (data_type=%s).", symbols, data_type)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("Failed to subscribe to %s", symbols)
            return False

    def unsubscribe(self, symbols: list[str], data_type: str = "SymbolUpdate") -> None:
        if self.socket is None or not self._is_open:
            return
        try:
            self.socket.unsubscribe(symbols=symbols, data_type=data_type)
            for s in symbols:
                self._dynamic_symbols.pop(s, None)
            logger.info("Unsubscribed from %s.", symbols)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to unsubscribe from %s", symbols)

    # -- health check --------------------------------------------------------
    def seconds_since_last_message(self) -> Optional[float]:
        if self._last_message_at is None:
            return None
        return time.time() - self._last_message_at

    @property
    def is_open(self) -> bool:
        return self._is_open
