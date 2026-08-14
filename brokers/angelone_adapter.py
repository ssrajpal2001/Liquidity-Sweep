"""
brokers/angelone_adapter.py

AngelOne SmartAPI adapter. Verified against the installed `smartapi-python`
SDK by introspection (SmartConnect's real method signatures, generateSession's
actual response shape, placeOrder's real param list) — same rigor as
Fyers, not guessed from docs alone, EXCEPT for two pieces flagged below
that genuinely could not be verified without a live account.

KEY DIFFERENCE FROM FYERS/UPSTOX: AngelOne login is TOTP-based
(generateSession(client_code, pin, totp_code)), not an OAuth browser
redirect. This means the bot can log itself in completely headlessly —
no daily manual browser step. auth_type = DIRECT_CREDENTIALS; the web UI
calls login() directly instead of the OAuth build_login_url()/
exchange_code() pair.

UNVERIFIED, NEEDS LIVE CONFIRMATION TOMORROW:
1. WebSocket tick shape — SmartWebSocketV2's on_data callback shape
   (field names for token/LTP) is based on community sample code, less
   certain than Fyers' (which had a directly-confirmed 'ltp'/'symbol'
   sample). Watch for [Unrecognized AngelOne tick shape] warnings.
2. Options trading-symbol format for searchScrip() — AngelOne's exact
   weekly-options symbol naming convention (date format, spacing) has
   changed historically and isn't confirmed here. get_option_chain()
   below documents the assumed format and logs clearly if a search
   returns nothing, rather than silently trading the wrong contract.

Every strike's Delta is computed locally via execution/greeks_engine.py
regardless — this adapter does not call AngelOne's optionGreek() endpoint
at all, deliberately, since its exact parameter contract wasn't
verifiable here and the whole point of the Delta engine is to not depend
on any broker's Greeks feed.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pyotp
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

from brokers.base import AuthType, BrokerAdapter, ConnectionCheckResult, OptionLeg, OrderResult, ReconnectCallback, TickCallback
from execution.greeks_engine import compute_delta_from_price

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Verified from SmartWebSocketV2's class constants (introspected on the
# installed SDK): exchangeType codes for the subscribe() token_list.
EXCHANGE_TYPE_NSE_FO = 2
EXCHANGE_TYPE_BSE_FO = 4
WS_MODE_LTP = 1

ORDER_STATUS_COMPLETE = "complete"


@dataclass
class AngelOneSession:
    jwt_token: str
    refresh_token: str
    feed_token: str
    client_code: str
    generated_at: str  # ISO 8601 IST

    def is_stale(self, max_age_hours: float = 20.0) -> bool:
        generated = datetime.fromisoformat(self.generated_at)
        return datetime.now(IST) - generated > timedelta(hours=max_age_hours)


class AngelOneSessionStore:
    """Same rationale as auth.auth.TokenStore, kept separate because
    AngelOne's session has three tokens (jwt/refresh/feed), not one."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Optional[AngelOneSession]:
        if not self.path.exists():
            return None
        try:
            return AngelOneSession(**json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            return None

    def save(self, session: AngelOneSession) -> None:
        self.path.write_text(json.dumps(asdict(session), indent=2), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


class AngelOneBrokerAdapter(BrokerAdapter):
    def __init__(self, env, paper_mode: bool):
        # `env` needs: api_key, client_code, pin, totp_secret, token_store_path
        self.env = env
        self.paper_mode = paper_mode
        self.session_store = AngelOneSessionStore(env.token_store_path)
        self._smart: Optional[SmartConnect] = None
        self._ws: Optional[SmartWebSocketV2] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_open = False
        self._last_message_at: Optional[float] = None
        self._on_tick: Optional[TickCallback] = None
        self._instrument_master_cache: Optional[list[dict]] = None

    @property
    def broker_name(self) -> str:
        return "angelone"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.DIRECT_CREDENTIALS

    @classmethod
    def required_credential_fields(cls) -> list[tuple[str, str, bool]]:
        return [
            ("api_key", "API Key", False),
            ("client_code", "Client Code", False),
            ("pin", "PIN (login password)", True),
            ("totp_secret", "TOTP Secret (the 'QR value' from Enable TOTP setup, not a 6-digit code)", True),
        ]

    # -- auth: direct TOTP login, no browser needed ----------------------------
    def login(self) -> ConnectionCheckResult:
        try:
            totp_code = pyotp.TOTP(self.env.totp_secret).now()
        except Exception as exc:  # noqa: BLE001
            return ConnectionCheckResult(ok=False, detail=f"Invalid TOTP secret: {exc}")

        smart = SmartConnect(api_key=self.env.api_key)
        try:
            result = smart.generateSession(self.env.client_code, self.env.pin, totp_code)
        except Exception as exc:  # noqa: BLE001
            logger.exception("AngelOne login failed.")
            return ConnectionCheckResult(ok=False, detail=str(exc))

        if not result or not result.get("status"):
            detail = result.get("message", "Unknown error") if result else "No response"
            logger.error("AngelOne login rejected: %s", detail)
            return ConnectionCheckResult(ok=False, detail=detail)

        data = result["data"]
        session = AngelOneSession(
            jwt_token=data["jwtToken"], refresh_token=data["refreshToken"],
            feed_token=data["feedToken"], client_code=self.env.client_code,
            generated_at=datetime.now(IST).isoformat(),
        )
        self.session_store.save(session)
        self._smart = smart
        user_name = result.get("data", {}).get("name")
        logger.info("[BROKER_LOGIN_OK] angelone client_code=%s name=%s", self.env.client_code, user_name)
        return ConnectionCheckResult(ok=True, detail="Logged in successfully.",
                                      user_name=user_name, user_id=self.env.client_code)

    def _get_smart(self) -> Optional[SmartConnect]:
        if self._smart is not None:
            return self._smart
        session = self.session_store.load()
        if session is None or session.is_stale():
            return None
        smart = SmartConnect(
            api_key=self.env.api_key, access_token=session.jwt_token,
            refresh_token=session.refresh_token, feed_token=session.feed_token,
        )
        self._smart = smart
        return smart

    def is_authenticated(self) -> bool:
        return self._get_smart() is not None

    def test_connection(self) -> ConnectionCheckResult:
        smart = self._get_smart()
        if smart is None:
            # AngelOne's genuine advantage: no browser needed, so a stale/
            # missing session can just re-login right here rather than
            # forcing the caller through a UI round trip.
            return self.login()
        try:
            session = self.session_store.load()
            profile = smart.getProfile(session.refresh_token if session else "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("AngelOne connectivity check failed, retrying via fresh login: %s", exc)
            return self.login()

        if not profile or not profile.get("status"):
            return self.login()  # stale session — headless re-login, no human needed

        data = profile.get("data", {})
        return ConnectionCheckResult(
            ok=True, detail="Authenticated successfully.",
            user_name=data.get("name"), user_id=data.get("clientcode"),
        )

    # -- market data feed -------------------------------------------------------
    def start_feed(self, symbols: list[str], on_tick: TickCallback, on_reconnect: Optional[ReconnectCallback] = None) -> None:
        smart = self._get_smart()
        if smart is None:
            raise RuntimeError("Cannot start feed — not authenticated. Call login() first.")
        session = self.session_store.load()
        self._on_tick = on_tick

        # symbols here are expected as "EXCHANGE_TYPE:TOKEN" pairs (e.g.
        # "2:26009") — the caller/session-manager is responsible for
        # resolving tradingsymbols to (exchangeType, token) via the
        # instrument master before calling start_feed(), since AngelOne's
        # feed subscribes by numeric token, not by symbol string.
        token_list = self._build_token_list(symbols)

        self._ws = SmartWebSocketV2(
            auth_token=session.jwt_token, api_key=self.env.api_key,
            client_code=self.env.client_code, feed_token=session.feed_token,
        )
        self._ws.on_open = lambda wsapp: self._handle_open(token_list)
        self._ws.on_data = self._handle_data
        self._ws.on_error = self._handle_error
        self._ws.on_close = self._handle_close

        # SmartWebSocketV2.connect() calls run_forever() directly and
        # BLOCKS — confirmed by introspecting the installed SDK, unlike
        # Fyers'/Upstox's SDKs which spawn their own thread. Must wrap
        # ourselves to keep start_feed() non-blocking.
        self._ws_thread = threading.Thread(target=self._ws.connect, daemon=True, name="angelone-ws")
        self._ws_thread.start()

    @staticmethod
    def _build_token_list(symbols: list[str]) -> list[dict]:
        by_exchange: dict[int, list[str]] = {}
        for s in symbols:
            exch_str, token = s.split(":", 1)
            by_exchange.setdefault(int(exch_str), []).append(token)
        return [{"exchangeType": exch, "tokens": tokens} for exch, tokens in by_exchange.items()]

    def _handle_open(self, token_list: list[dict]) -> None:
        self._ws_open = True
        logger.info("AngelOne WS connected.")
        try:
            self._ws.subscribe("liquidity-sweep-bot", WS_MODE_LTP, token_list)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to subscribe on AngelOne WS open.")

    def _handle_data(self, wsapp, message) -> None:
        self._last_message_at = time.time()
        # Field names below are the community-sample-code best guess, NOT
        # independently confirmed — see module docstring caveat #1.
        token = None
        ltp = None
        if isinstance(message, dict):
            token = message.get("token")
            raw_ltp = message.get("last_traded_price")
            if raw_ltp is not None:
                ltp = raw_ltp / 100.0  # AngelOne reports price in paise
        if token is None or ltp is None:
            logger.warning(
                "[Unrecognized AngelOne tick shape] please share this line from "
                "logs/bot.log so the handler can be adjusted: %s", message,
            )
            return
        if self._on_tick is not None:
            self._on_tick(token, ltp)

    def _handle_error(self, wsapp, error) -> None:
        logger.error("AngelOne WS error: %s", error)

    def _handle_close(self, wsapp) -> None:
        self._ws_open = False
        logger.warning("AngelOne WS closed.")

    def stop_feed(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close_connection()
            except Exception:  # noqa: BLE001
                logger.exception("Error closing AngelOne WS.")

    def subscribe(self, symbols: list[str]) -> bool:
        if self._ws is None or not self._ws_open:
            return False
        try:
            self._ws.subscribe("liquidity-sweep-bot", WS_MODE_LTP, self._build_token_list(symbols))
            return True
        except Exception:  # noqa: BLE001
            logger.exception("Failed to subscribe to %s", symbols)
            return False

    def unsubscribe(self, symbols: list[str]) -> None:
        if self._ws is None or not self._ws_open:
            return
        try:
            self._ws.unsubscribe("liquidity-sweep-bot", WS_MODE_LTP, self._build_token_list(symbols))
        except Exception:  # noqa: BLE001
            logger.exception("Failed to unsubscribe from %s", symbols)

    def seconds_since_last_message(self) -> Optional[float]:
        if self._last_message_at is None:
            return None
        return time.time() - self._last_message_at

    @property
    def is_feed_open(self) -> bool:
        return self._ws_open

    # -- options & expiry --------------------------------------------------------
    def nearest_expiry(self, underlying_symbol: str) -> Optional[str]:
        # AngelOne has no dedicated "list expiries" endpoint in this SDK —
        # expiry resolution goes through the same instrument-master lookup
        # as strike search (see get_option_chain's caveat). Returning None
        # here signals "caller must resolve expiry externally" until the
        # instrument-master integration is verified live.
        logger.warning(
            "AngelOne nearest_expiry() not implemented — needs the instrument "
            "master file integration (see module docstring caveat #2)."
        )
        return None

    def get_option_chain(self, underlying_symbol: str, expiry: str) -> list[OptionLeg]:
        smart = self._get_smart()
        if smart is None:
            return []
        try:
            result = smart.searchScrip("NFO", underlying_symbol)
        except Exception:  # noqa: BLE001
            logger.exception("AngelOne searchScrip failed for %s", underlying_symbol)
            return []

        if not result or not result.get("status"):
            logger.warning(
                "[ANGELONE_SYMBOL_SEARCH_EMPTY] searchScrip('NFO', %s) returned nothing — "
                "the assumed symbol-naming convention (see module docstring caveat #2) "
                "likely needs adjusting once tested against a live account.",
                underlying_symbol,
            )
            return []

        legs: list[OptionLeg] = []
        for row in result.get("data", []):
            trading_symbol = row.get("tradingsymbol", "")
            if not trading_symbol.endswith(("CE", "PE")):
                continue
            option_type = trading_symbol[-2:]
            try:
                ltp_resp = smart.ltpData("NFO", trading_symbol, row.get("symboltoken"))
                ltp = float(ltp_resp["data"]["ltp"])
            except Exception:  # noqa: BLE001
                continue
            legs.append(OptionLeg(
                symbol=trading_symbol, strike_price=0.0,  # strike parsed from symbol if needed by caller
                option_type=option_type, ltp=ltp, delta=None,  # always None — computed locally, see docstring
            ))
        return legs

    # -- orders --------------------------------------------------------------
    def place_entry_buy(self, symbol: str, quantity: int, current_ask: float, tag: str) -> OrderResult:
        return self._place(symbol, quantity, current_ask, "BUY", tag)

    def place_exit_sell(self, symbol: str, quantity: int, limit_price: float, tag: str) -> OrderResult:
        return self._place(symbol, quantity, limit_price, "SELL", tag)

    def _place(self, symbol: str, quantity: int, price: float, transaction_type: str, tag: str) -> OrderResult:
        description = f"{transaction_type} {quantity}x {symbol} @ {price}"
        if self.paper_mode:
            order_id = f"PAPER-ANGELONE-{int(time.time())}"
            logger.info("[PAPER_ORDER_SIMULATED] %s order_id=%s (no real order sent to AngelOne)",
                        description, order_id)
            return OrderResult(ok=True, order_id=order_id, detail="Paper trade simulated.")

        smart = self._get_smart()
        if smart is None:
            return OrderResult(ok=False, order_id=None, detail="Not authenticated.")

        # symboltoken is required by AngelOne's placeOrder but not carried
        # on our normalized `symbol` string — this needs the same
        # instrument-master lookup flagged in get_option_chain(). Left as
        # an explicit TODO rather than silently omitted.
        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "transactiontype": transaction_type,
            "exchange": "NFO",
            "ordertype": "LIMIT",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": str(round(price, 2)),
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(quantity),
        }
        try:
            order_id = smart.placeOrder(order_params)
        except Exception as exc:  # noqa: BLE001
            logger.exception("AngelOne order failed (%s)", description)
            return OrderResult(ok=False, order_id=None, detail=str(exc))

        if order_id is None:
            return OrderResult(ok=False, order_id=None, detail="placeOrder returned None — check logs.")
        logger.info("Order placed OK (%s): order_id=%s", description, order_id)
        return OrderResult(ok=True, order_id=order_id, detail="Placed successfully.")

    def get_order_status(self, order_id: str) -> dict:
        if order_id.startswith("PAPER-"):
            return {"status": "complete", "filled_quantity": 0, "average_price": 0.0}

        smart = self._get_smart()
        if smart is None:
            return {"status": "unknown"}
        try:
            response = smart.orderBook()
        except Exception:  # noqa: BLE001
            logger.exception("AngelOne order status fetch failed for %s", order_id)
            return {"status": "unknown"}

        if not response or not response.get("status"):
            return {"status": "unknown"}
        for order in response.get("data") or []:
            if str(order.get("orderid")) == str(order_id):
                raw_status = (order.get("status") or "").lower()
                status = "complete" if raw_status == "complete" else (
                    "rejected" if raw_status in ("rejected", "cancelled") else "pending"
                )
                return {
                    "status": status,
                    "filled_quantity": int(order.get("filledshares", 0) or 0),
                    "average_price": float(order.get("averageprice", 0) or 0),
                }
        return {"status": "unknown"}
