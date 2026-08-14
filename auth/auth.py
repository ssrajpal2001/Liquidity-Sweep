"""
auth/auth.py

Upstox OAuth 2.0 daily token lifecycle: build the login URL, exchange the
authorization code for an access token, persist it securely, and check
expiry before every session. Everything downstream (REST client, WS client)
gets its token exclusively through UpstoxAuth.get_valid_token() — nothing
else should read the token file or call the login/token endpoints directly.

IMPORTANT — naming note: Upstox's market-data, historical-candle, and order
APIs have a distinct "v3" generation, but the *login/token* endpoints have
not been separately versioned — they still live under /v2/login/... even
for apps calling v3 market/order endpoints. This module targets the
endpoints Upstox's docs currently document for authentication; if Upstox
introduces a dedicated v3 auth endpoint later, only AUTHORIZE_URL and
TOKEN_URL below need to change.

IMPORTANT — no refresh tokens: Upstox does not issue a refresh_token. Every
access token expires at the next 3:30 AM IST after it was generated,
whatever time it was generated, and getting a new one always requires a
fresh interactive browser login (the user completes Upstox's own login
dialog once and hands back an authorization code). This module makes that
one daily manual step as small as possible; it cannot and does not attempt
to automate away the login itself — scripting real login credentials
against Upstox's login page is unsafe and against the spirit of Upstox's
"all logins are handled by upstox.com" design.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import urllib.parse as urlparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

AUTHORIZE_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"

REQUEST_TIMEOUT_SECONDS = 15


class AuthError(RuntimeError):
    """Raised for any authentication / token-lifecycle failure."""


class ReauthRequired(AuthError):
    """Raised when there is no valid token and an interactive login is needed."""


def compute_expiry(generated_at: datetime) -> datetime:
    """Upstox access tokens always expire at the *next* 3:30 AM IST after
    generation — a token generated at 8 PM Tuesday expires 3:30 AM Wednesday;
    a token generated at 2:30 AM Wednesday still expires 3:30 AM the *same*
    Wednesday (per Upstox's Get Token API docs). Both cases reduce to: find
    the next occurrence of 03:30 IST strictly after generated_at.
    """
    generated_at = generated_at.astimezone(IST)
    candidate = generated_at.replace(hour=3, minute=30, second=0, microsecond=0)
    if candidate <= generated_at:
        candidate += timedelta(days=1)
    return candidate


@dataclass
class TokenRecord:
    access_token: str
    generated_at: str  # ISO 8601, IST
    expires_at: str    # ISO 8601, IST
    user_id: Optional[str] = None
    email: Optional[str] = None

    def is_expired(self, at: Optional[datetime] = None) -> bool:
        now = (at or datetime.now(IST)).astimezone(IST)
        return now >= datetime.fromisoformat(self.expires_at)


class TokenStore:
    """Persists the daily token to a local JSON file with owner-only
    permissions. Swap this for a Redis/SQLite/secrets-manager backend later
    without changing UpstoxAuth's interface — load()/save()/clear() is the
    whole contract."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Optional[TokenRecord]:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return TokenRecord(**data)
        except Exception as exc:  # noqa: BLE001 — corrupt/partial file, treat as absent
            logger.warning("Could not read token store at %s: %s", self.path, exc)
            return None

    def save(self, record: TokenRecord) -> None:
        self.path.write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)  # owner read/write only
        except OSError:
            logger.debug("chmod not supported on this platform; skipping.")

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


class UpstoxAuth:
    """Owns the full daily token lifecycle for one Upstox app."""

    def __init__(self, env, store: Optional[TokenStore] = None):
        # `env` is a config.config_loader.EnvConfig — typed loosely here to
        # avoid a circular import between config and auth at module load time.
        self.env = env
        self.store = store or TokenStore(env.token_store_path)

    # -- Step 1: interactive login -------------------------------------------
    def build_login_url(self, state: Optional[str] = None) -> str:
        state = state or secrets.token_urlsafe(16)
        params = {
            "response_type": "code",
            "client_id": self.env.api_key,
            "redirect_uri": self.env.redirect_uri,
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlparse.urlencode(params)}"

    @staticmethod
    def extract_code(code_or_redirect_url: str) -> str:
        """Accepts either a bare authorization code or the full URL the
        browser was redirected to, and returns just the `code` value."""
        parsed = urlparse.urlparse(code_or_redirect_url)
        qs = urlparse.parse_qs(parsed.query)
        if "code" in qs:
            return qs["code"][0]
        return code_or_redirect_url.strip()

    # -- Step 2: exchange code for token --------------------------------------
    def exchange_code(self, code_or_redirect_url: str) -> TokenRecord:
        code = self.extract_code(code_or_redirect_url)
        if not code:
            raise AuthError("No authorization code found in input.")

        payload = {
            "code": code,
            "client_id": self.env.api_key,
            "client_secret": self.env.api_secret,
            "redirect_uri": self.env.redirect_uri,
            "grant_type": "authorization_code",
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }

        try:
            resp = requests.post(
                TOKEN_URL, data=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            raise AuthError(f"Network error during token exchange: {exc}") from exc

        if resp.status_code != 200:
            # The authorization code is single-use regardless of outcome —
            # a failed exchange means the user must restart from build_login_url().
            raise AuthError(
                f"Token exchange failed (HTTP {resp.status_code}): {resp.text}"
            )

        data = resp.json()
        access_token = data.get("access_token")
        if not access_token:
            raise AuthError(f"Token exchange response missing access_token: {data}")

        generated_at = datetime.now(IST)
        expires_at = compute_expiry(generated_at)
        record = TokenRecord(
            access_token=access_token,
            generated_at=generated_at.isoformat(),
            expires_at=expires_at.isoformat(),
            user_id=data.get("user_id"),
            email=data.get("email"),
        )
        self.store.save(record)
        logger.info(
            "New Upstox access token stored (user_id=%s). Expires %s IST.",
            record.user_id,
            expires_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
        return record

    # -- Step 3: what every other module calls --------------------------------
    def get_valid_token(self) -> str:
        """Returns a valid access token, or raises ReauthRequired with a
        message telling the caller exactly how to fix it. Callers should not
        catch this and silently continue — an expired/missing token means
        the bot must not place any orders."""
        record = self.store.load()
        if record is None:
            raise ReauthRequired(
                "No stored Upstox token found. Run: python -m auth.auth login"
            )
        if record.is_expired():
            raise ReauthRequired(
                f"Stored Upstox token expired at {record.expires_at} (IST). "
                "Run: python -m auth.auth login"
            )
        return record.access_token

    def is_authenticated(self) -> bool:
        try:
            self.get_valid_token()
            return True
        except ReauthRequired:
            return False

    def time_to_expiry(self) -> Optional[timedelta]:
        """None if there is no stored token; otherwise how long until it
        expires (negative if already expired)."""
        record = self.store.load()
        if record is None:
            return None
        return datetime.fromisoformat(record.expires_at) - datetime.now(IST)


# -- CLI: `python -m auth.auth login` / `python -m auth.auth check` -----------

def _run_interactive_login(env) -> None:
    auth = UpstoxAuth(env)
    url = auth.build_login_url()
    print("1. Open this URL in a browser and log in to Upstox:\n")
    print(f"   {url}\n")
    print("2. After login, Upstox redirects you to your redirect_uri.")
    print("   Paste the FULL redirected URL below (or just the `code` value).\n")
    pasted = input("Redirect URL / code: ").strip()
    record = auth.exchange_code(pasted)
    print(f"\nToken stored at: {auth.store.path}")
    print(f"Valid until    : {record.expires_at} (IST)")


def _run_check(env) -> None:
    auth = UpstoxAuth(env)
    if auth.is_authenticated():
        remaining = auth.time_to_expiry()
        print(f"Token is valid. Time to expiry: {remaining}")
    else:
        print("No valid token. Run: python -m auth.auth login")


if __name__ == "__main__":
    import sys

    from config.config_loader import load_settings
    from config.logging_setup import setup_logging

    settings = load_settings()
    setup_logging(level=settings.raw.get("logging", {}).get("level", "INFO"))

    command = sys.argv[1] if len(sys.argv) > 1 else "check"
    if command == "login":
        _run_interactive_login(settings.env)
    elif command == "check":
        _run_check(settings.env)
    else:
        print("Usage: python -m auth.auth [login|check]")
