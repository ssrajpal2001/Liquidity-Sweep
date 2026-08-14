"""
auth/auth.py

Fyers daily token lifecycle: build the login URL, exchange the auth code
for an access token (Fyers uses a SHA-256 hash of "client_id:secret_key"
in the exchange, not a plain client_secret POST field — verified against
the installed fyers-apiv3 SDK's SessionModel.generate_token/get_hash),
persist it securely, and check it before every session.

IMPORTANT — token lifetime is NOT a fixed documented clock time like
Upstox's 3:30 AM IST rule. Community reports consistently say "valid for
one day" but disagree on the exact cutoff. Rather than hardcode a wrong
assumption, this module treats a stored token as a candidate and defers
to a live API call (UpstoxRestClient-equivalent: FyersRestClient.
test_connection(), which calls get_profile()) to confirm it's actually
still accepted — that's the authoritative check, not the clock.
compute_soft_expiry() below is only a conservative backstop (24h) so a
long-idle process doesn't try a token that's almost certainly dead
without even checking.

Fyers also issues a refresh_token (documented ~15 day validity) that can
mint a new access_token without a full interactive login, but the
installed SDK version has no wrapped method for it and the raw endpoint
isn't verified here — noted as a fast-follow rather than guessed at.
"""
from __future__ import annotations

import hashlib
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

# Verified against fyers_apiv3.fyersModel.Config on the installed SDK.
AUTHORIZE_URL = "https://api-t1.fyers.in/api/v3/generate-authcode"
TOKEN_URL = "https://api-t1.fyers.in/api/v3/validate-authcode"

REQUEST_TIMEOUT_SECONDS = 15
SOFT_EXPIRY_HOURS = 20  # conservative backstop; see module docstring


class AuthError(RuntimeError):
    """Raised for any authentication / token-lifecycle failure."""


class ReauthRequired(AuthError):
    """Raised when there is no usable token and an interactive login is needed."""


def compute_soft_expiry(generated_at: datetime) -> datetime:
    """Conservative backstop only — see module docstring. The real check
    is FyersRestClient.test_connection() against the live API."""
    return generated_at.astimezone(IST) + timedelta(hours=SOFT_EXPIRY_HOURS)


@dataclass
class TokenRecord:
    access_token: str          # Fyers wants "client_id:access_token" when used — see combined_token()
    generated_at: str          # ISO 8601, IST
    soft_expires_at: str       # ISO 8601, IST — backstop only, not authoritative
    client_id: Optional[str] = None

    def is_soft_expired(self, at: Optional[datetime] = None) -> bool:
        now = (at or datetime.now(IST)).astimezone(IST)
        return now >= datetime.fromisoformat(self.soft_expires_at)

    def combined_token(self) -> str:
        """FyersModel/FyersDataSocket both expect 'client_id:access_token',
        not the bare token — verified against fyers_apiv3 sample code."""
        return f"{self.client_id}:{self.access_token}"


class TokenStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Optional[TokenRecord]:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return TokenRecord(**data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read token store at %s: %s", self.path, exc)
            return None

    def save(self, record: TokenRecord) -> None:
        self.path.write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            logger.debug("chmod not supported on this platform; skipping.")

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


class FyersAuth:
    def __init__(self, env, store: Optional[TokenStore] = None):
        # `env` is a config.config_loader.EnvConfig.
        self.env = env
        self.store = store or TokenStore(env.token_store_path)

    # -- Step 1: interactive login -------------------------------------------
    def build_login_url(self, state: Optional[str] = None) -> str:
        state = state or secrets.token_urlsafe(16)
        params = {
            "client_id": self.env.client_id,
            "redirect_uri": self.env.redirect_uri,
            "response_type": "code",
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlparse.urlencode(params)}"

    @staticmethod
    def extract_code(code_or_redirect_url: str) -> str:
        parsed = urlparse.urlparse(code_or_redirect_url)
        qs = urlparse.parse_qs(parsed.query)
        if "auth_code" in qs:
            return qs["auth_code"][0]
        if "code" in qs:
            return qs["code"][0]
        return code_or_redirect_url.strip()

    # -- Step 2: exchange code for token (hash-based, per Fyers' SessionModel) --
    def exchange_code(self, code_or_redirect_url: str) -> TokenRecord:
        code = self.extract_code(code_or_redirect_url)
        if not code:
            raise AuthError("No authorization code found in input.")

        app_id_hash = hashlib.sha256(
            f"{self.env.client_id}:{self.env.secret_key}".encode()
        ).hexdigest()

        payload = {
            "grant_type": "authorization_code",
            "appIdHash": app_id_hash,
            "code": code,
        }
        try:
            resp = requests.post(TOKEN_URL, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise AuthError(f"Network error during token exchange: {exc}") from exc

        data = resp.json() if resp.content else {}
        if resp.status_code != 200 or data.get("s") != "ok":
            raise AuthError(f"Token exchange failed (HTTP {resp.status_code}): {data}")

        access_token = data.get("access_token")
        if not access_token:
            raise AuthError(f"Token exchange response missing access_token: {data}")

        generated_at = datetime.now(IST)
        record = TokenRecord(
            access_token=access_token,
            generated_at=generated_at.isoformat(),
            soft_expires_at=compute_soft_expiry(generated_at).isoformat(),
            client_id=self.env.client_id,
        )
        self.store.save(record)
        logger.info(
            "New Fyers access token stored (client_id=%s). Soft backstop expiry: %s IST "
            "— actual validity confirmed via live get_profile() check, not this timestamp.",
            record.client_id, record.soft_expires_at,
        )
        return record

    # -- Step 3: what every other module calls --------------------------------
    def get_valid_token_record(self) -> TokenRecord:
        record = self.store.load()
        if record is None:
            raise ReauthRequired("No stored Fyers token found. Run: python -m auth.auth login")
        if record.is_soft_expired():
            raise ReauthRequired(
                f"Stored Fyers token is past its {SOFT_EXPIRY_HOURS}h soft backstop "
                f"(generated {record.generated_at}). Run: python -m auth.auth login"
            )
        return record

    def get_valid_token(self) -> str:
        """Returns the combined 'client_id:access_token' string the Fyers
        SDK expects."""
        return self.get_valid_token_record().combined_token()

    def is_authenticated(self) -> bool:
        try:
            self.get_valid_token_record()
            return True
        except ReauthRequired:
            return False


# -- CLI: `python -m auth.auth login` / `python -m auth.auth check` -----------

def _run_interactive_login(env) -> None:
    auth = FyersAuth(env)
    url = auth.build_login_url()
    print("1. Open this URL in a browser and log in to Fyers:\n")
    print(f"   {url}\n")
    print("2. After login, Fyers redirects you to your redirect_uri with")
    print("   ?auth_code=... in the URL. Paste the FULL redirected URL below")
    print("   (or just the auth_code value).\n")
    pasted = input("Redirect URL / auth_code: ").strip()
    record = auth.exchange_code(pasted)
    print(f"\nToken stored at: {auth.store.path}")
    print(f"Soft backstop expiry: {record.soft_expires_at} (IST) — run "
          f"'python main.py' to confirm it's actually accepted right now.")


def _run_check(env) -> None:
    auth = FyersAuth(env)
    if auth.is_authenticated():
        print("Token present and within its soft backstop window "
              "(this does NOT guarantee Fyers still accepts it — "
              "run main.py's connectivity check for that).")
    else:
        print("No usable token. Run: python -m auth.auth login")


if __name__ == "__main__":
    import sys

    from config.config_loader import load_settings

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()

    command = sys.argv[1] if len(sys.argv) > 1 else "check"
    if command == "login":
        _run_interactive_login(settings.env)
    elif command == "check":
        _run_check(settings.env)
    else:
        print("Usage: python -m auth.auth [login|check]")
