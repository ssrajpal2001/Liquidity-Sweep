"""
brokers/fyers_adapter.py

Wraps the existing Fyers-specific modules (auth.FyersAuth,
data_feed.fyers_rest_client.FyersRestClient, data_feed.fyers_ws_client.
FyersWSClient, execution.option_selector.OptionSelector, execution.
order_manager.OrderManager, execution.expiry_resolver.ExpiryResolver)
behind the BrokerAdapter interface. Nothing about those modules'
internals changed — this is purely an adapter/facade layer, which is why
it was safe to add without touching or re-testing the Fyers-specific
plumbing itself.

This is the reference implementation for what a second broker adapter
(e.g. brokers/upstox_adapter.py, resurrecting the earlier Upstox modules)
would need to match.
"""
from __future__ import annotations

import logging
from typing import Optional

from auth.auth import FyersAuth, ReauthRequired
from brokers.base import AuthType, BrokerAdapter, ConnectionCheckResult, OptionLeg, OrderResult, ReconnectCallback, TickCallback
from data_feed.fyers_rest_client import FyersRestClient
from data_feed.fyers_ws_client import FyersWSClient
from execution.expiry_resolver import ExpiryResolver
from execution.option_selector import OptionSelector
from execution.order_manager import OrderManager as FyersOrderManager

logger = logging.getLogger(__name__)


class FyersBrokerAdapter(BrokerAdapter):
    def __init__(self, env, paper_mode: bool):
        # `env` is a config.config_loader.EnvConfig with FYERS_* fields.
        self.env = env
        self.paper_mode = paper_mode
        self.auth = FyersAuth(env)
        self.rest_client = FyersRestClient(env, auth=self.auth)
        # These all need an authenticated model, which doesn't exist until
        # after login — build them lazily on first use, not here, so
        # constructing the adapter never requires a token.
        self._expiry_resolver: Optional[ExpiryResolver] = None
        self._order_manager: Optional[FyersOrderManager] = None
        self._ws_client: Optional[FyersWSClient] = None
        self._option_selectors: dict[str, OptionSelector] = {}  # underlying -> selector, built lazily

    @property
    def expiry_resolver(self) -> ExpiryResolver:
        if self._expiry_resolver is None:
            self._expiry_resolver = ExpiryResolver(self.rest_client.model)
        return self._expiry_resolver

    @property
    def order_manager(self) -> FyersOrderManager:
        if self._order_manager is None:
            self._order_manager = FyersOrderManager(self.rest_client.model, paper_mode=self.paper_mode)
        return self._order_manager

    @property
    def broker_name(self) -> str:
        return "fyers"

    @property
    def auth_type(self):
        return AuthType.OAUTH_REDIRECT

    @classmethod
    def required_credential_fields(cls) -> list[tuple[str, str, bool]]:
        return [
            ("client_id", "Client ID (include the -100 suffix, e.g. XC1234-100)", False),
            ("secret_key", "Secret Key", True),
            ("redirect_uri", "Redirect URI (must exactly match your Fyers app)", False),
        ]

    # -- auth --------------------------------------------------------------
    def build_login_url(self, state: Optional[str] = None) -> str:
        return self.auth.build_login_url(state)

    def exchange_code(self, code_or_redirect_url: str) -> None:
        self.auth.exchange_code(code_or_redirect_url)
        self.rest_client.refresh_client()

    def is_authenticated(self) -> bool:
        return self.auth.is_authenticated()

    def test_connection(self) -> ConnectionCheckResult:
        result = self.rest_client.test_connection()
        return ConnectionCheckResult(
            ok=result.ok, detail=result.detail, user_name=result.user_name, user_id=result.user_id
        )

    # -- market data feed -------------------------------------------------------
    def start_feed(
        self, symbols: list[str], on_tick: TickCallback, on_reconnect: Optional[ReconnectCallback] = None
    ) -> None:
        try:
            combined_token = self.auth.get_valid_token()
        except ReauthRequired as exc:
            raise RuntimeError(f"Cannot start feed — not authenticated: {exc}") from exc

        self._ws_client = FyersWSClient(
            combined_token=combined_token, symbols=symbols, on_tick=on_tick,
            on_reconnect=on_reconnect, litemode=True,
        )
        self._ws_client.start()

    def stop_feed(self) -> None:
        if self._ws_client is not None:
            self._ws_client.stop()

    def subscribe(self, symbols: list[str]) -> bool:
        if self._ws_client is None:
            return False
        return self._ws_client.subscribe(symbols)

    def unsubscribe(self, symbols: list[str]) -> None:
        if self._ws_client is not None:
            self._ws_client.unsubscribe(symbols)

    def seconds_since_last_message(self) -> Optional[float]:
        if self._ws_client is None:
            return None
        return self._ws_client.seconds_since_last_message()

    @property
    def is_feed_open(self) -> bool:
        return self._ws_client is not None and self._ws_client.is_open

    # -- options & expiry --------------------------------------------------------
    def nearest_expiry(self, underlying_symbol: str) -> Optional[str]:
        return self.expiry_resolver.nearest_expiry(underlying_symbol)

    def get_option_chain(self, underlying_symbol: str, expiry: str) -> list[OptionLeg]:
        selector = self._option_selectors.setdefault(
            underlying_symbol,
            OptionSelector(self.rest_client.model, underlying_symbol, strike_interval=1),
        )
        chain = selector._fetch_chain(expiry)  # noqa: SLF001 — adapter is allowed inside the seam
        legs = []
        for row in chain:
            try:
                legs.append(OptionLeg(
                    symbol=row["symbol"], strike_price=float(row["strike_price"]),
                    option_type=row["option_type"], ltp=float(row.get("ltp", 0)),
                    delta=float(row["delta"]) if row.get("delta") not in (None, "", 0) else None,
                ))
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping malformed option chain row: %s", row)
        return legs

    # -- orders --------------------------------------------------------------
    def place_entry_buy(self, symbol: str, quantity: int, current_ask: float, tag: str) -> OrderResult:
        r = self.order_manager.place_entry_buy(symbol, quantity, current_ask, tag)
        return OrderResult(ok=r.ok, order_id=r.order_id, detail=r.detail)

    def place_exit_sell(self, symbol: str, quantity: int, limit_price: float, tag: str) -> OrderResult:
        r = self.order_manager.place_exit_sell(symbol, quantity, limit_price, tag)
        return OrderResult(ok=r.ok, order_id=r.order_id, detail=r.detail)

    def get_order_status(self, order_id: str) -> dict:
        return self.order_manager.get_order_status(order_id)
