"""
brokers/base.py

The plug-and-play contract. Every broker (Fyers today, Upstox/Zerodha/
anyone else tomorrow) implements this ONE interface. Nothing in
strategy/, execution/risk_engine.py, execution/position_manager.py, or
risk_controls/ ever imports a broker-specific module directly — they only
ever talk to a `BrokerAdapter`. That's what makes "attach/detach a
broker" possible without touching strategy code: swapping the concrete
adapter class is the entire integration.

Design notes:
- All broker-specific symbol formats, auth flows, tick shapes, and order
  payloads are hidden behind this interface. Callers work in the
  normalized types below (Tick, OptionLeg, OrderResult) regardless of
  which broker is underneath.
- Delta on OptionLeg is Optional — a broker MAY supply it, but nothing
  downstream should assume it will. execution/option_selector.py falls
  back to execution/greeks_engine.py's broker-independent Black-Scholes
  calculation whenever a leg's delta is None. This is the fix for "what
  if a broker doesn't give Greeks" — it's now a per-broker detail, not an
  application-breaking assumption.
- One BrokerAdapter instance = one authenticated session for one client's
  one broker connection. A multi-client system holds one adapter instance
  per (client, broker) pair — see the module docstring in
  brokers/registry.py for how instances get created and looked up.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

TickCallback = Callable[[str, float], None]        # (symbol, ltp)
ReconnectCallback = Callable[[], None]


class AuthType(str, Enum):
    OAUTH_REDIRECT = "oauth_redirect"          # Fyers, Upstox, Zerodha — browser login required
    DIRECT_CREDENTIALS = "direct_credentials"  # AngelOne (TOTP), some others — bot can log in headlessly


@dataclass
class ConnectionCheckResult:
    ok: bool
    detail: str
    user_name: Optional[str] = None
    user_id: Optional[str] = None


@dataclass
class OptionLeg:
    symbol: str                    # broker-specific tradeable symbol
    strike_price: float
    option_type: str               # "CE" or "PE"
    ltp: float
    delta: Optional[float]         # None if the broker doesn't supply it — caller must handle this
    expiry_epoch_seconds: Optional[float] = None  # needed if the caller computes Delta itself


@dataclass
class OrderResult:
    ok: bool
    order_id: Optional[str]
    detail: str


class BrokerAdapter(ABC):
    """Every method here mirrors what main.py's TradingSession already
    calls today against Fyers-specific classes — this interface is the
    generalization of that existing call pattern, not a new design."""

    # -- identity --------------------------------------------------------------
    @property
    @abstractmethod
    def broker_name(self) -> str:
        """Short machine-readable id, e.g. 'fyers', 'upstox' — used as the
        registry key and in logs/credential storage."""

    @classmethod
    @abstractmethod
    def required_credential_fields(cls) -> list[tuple[str, str, bool]]:
        """(field_name, display_label, is_secret) for every credential
        this broker needs. Drives the web UI's dynamic credential form —
        adding a broker never requires touching the UI code, just
        returning the right fields here."""

    @property
    @abstractmethod
    def auth_type(self) -> AuthType:
        """Determines which login method the web UI calls: OAUTH_REDIRECT
        brokers use build_login_url()/exchange_code() (a browser round
        trip); DIRECT_CREDENTIALS brokers use login() (no browser needed
        at all, credentials already in hand from the vault)."""

    # -- auth: OAUTH_REDIRECT brokers implement these -----------------------------
    def build_login_url(self, state: Optional[str] = None) -> str:
        raise NotImplementedError(f"{self.broker_name} does not use OAuth redirect login.")

    def exchange_code(self, code_or_redirect_url: str) -> None:
        raise NotImplementedError(f"{self.broker_name} does not use OAuth redirect login.")

    # -- auth: DIRECT_CREDENTIALS brokers implement this instead ------------------
    def login(self) -> ConnectionCheckResult:
        """Logs in directly using credentials already stored in the vault
        (e.g. AngelOne: client_code + PIN + a fresh TOTP code derived from
        a stored secret) — no browser redirect. Only meaningful when
        auth_type is DIRECT_CREDENTIALS."""
        raise NotImplementedError(f"{self.broker_name} uses OAuth redirect login instead — call build_login_url().")

    @abstractmethod
    def is_authenticated(self) -> bool:
        ...

    @abstractmethod
    def test_connection(self) -> ConnectionCheckResult:
        """Live check that the current token is actually accepted right
        now — authoritative over any locally-stored expiry guess."""

    # -- market data feed -------------------------------------------------------
    @abstractmethod
    def start_feed(
        self,
        symbols: list[str],
        on_tick: TickCallback,
        on_reconnect: Optional[ReconnectCallback] = None,
    ) -> None:
        """Non-blocking — connects the WebSocket and begins delivering
        ticks via on_tick(symbol, ltp) on a background thread."""

    @abstractmethod
    def stop_feed(self) -> None:
        ...

    @abstractmethod
    def subscribe(self, symbols: list[str]) -> bool:
        """Adds symbols to a running feed (e.g. an option leg once a
        position opens). Returns False if the feed isn't open yet."""

    @abstractmethod
    def unsubscribe(self, symbols: list[str]) -> None:
        ...

    @abstractmethod
    def seconds_since_last_message(self) -> Optional[float]:
        ...

    @property
    @abstractmethod
    def is_feed_open(self) -> bool:
        ...

    # -- options & expiry --------------------------------------------------------
    @abstractmethod
    def nearest_expiry(self, underlying_symbol: str) -> Optional[str]:
        ...

    @abstractmethod
    def get_option_chain(self, underlying_symbol: str, expiry: str) -> list[OptionLeg]:
        """Returns every CE/PE leg for the given expiry. delta may be None
        per-leg — callers must use execution/greeks_engine.py as a
        fallback rather than assuming it's always populated."""

    # -- orders --------------------------------------------------------------
    @abstractmethod
    def place_entry_buy(self, symbol: str, quantity: int, current_ask: float, tag: str) -> OrderResult:
        ...

    @abstractmethod
    def place_exit_sell(self, symbol: str, quantity: int, limit_price: float, tag: str) -> OrderResult:
        ...

    @abstractmethod
    def get_order_status(self, order_id: str) -> dict:
        """Returns {"status": "complete"|"pending"|"rejected"|"unknown",
        "filled_quantity": int, "average_price": float} — the normalized
        vocabulary execution/position_manager.py already expects."""
