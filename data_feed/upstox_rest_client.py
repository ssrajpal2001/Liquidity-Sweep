"""
data_feed/upstox_rest_client.py

Thin wrapper around the official `upstox-python` SDK's REST layer.

Phase 1 scope: build one authenticated SDK client (sandbox or live, per
settings.yaml/.env) and expose test_connection() to verify the daily token
actually works end-to-end before the bot is trusted to trade.

Phase 2 will extend this module with the historical-candle bootstrap and
missed-candle resync calls described in the work plan; this file is where
those methods belong, so later phases only add methods here rather than
introducing a second REST client.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

try:
    import upstox_client
    from upstox_client.rest import ApiException
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The 'upstox-python' SDK is required. Install it with:\n"
        "    pip install upstox-python-sdk --break-system-packages"
    ) from exc

from auth.auth import ReauthRequired, UpstoxAuth

logger = logging.getLogger(__name__)


@dataclass
class ConnectionCheckResult:
    ok: bool
    detail: str
    user_name: Optional[str] = None
    user_id: Optional[str] = None


class UpstoxRestClient:
    """Owns one authenticated upstox_client.ApiClient and hands it to
    whichever SDK API class (UserApi, HistoryApi, OptionsApi, OrderApiV3,
    ...) a given call needs."""

    def __init__(self, env, auth: Optional[UpstoxAuth] = None):
        # `env` is a config.config_loader.EnvConfig.
        self.env = env
        self.auth = auth or UpstoxAuth(env)
        self._api_client: Optional["upstox_client.ApiClient"] = None

    def _build_api_client(self) -> "upstox_client.ApiClient":
        access_token = self.auth.get_valid_token()  # raises ReauthRequired if none/expired
        configuration = upstox_client.Configuration(sandbox=self.env.sandbox)
        configuration.access_token = access_token
        return upstox_client.ApiClient(configuration)

    @property
    def api_client(self) -> "upstox_client.ApiClient":
        if self._api_client is None:
            self._api_client = self._build_api_client()
        return self._api_client

    def refresh_client(self) -> None:
        """Force the next api_client access to rebuild from the current
        stored token — call this right after a fresh login/token exchange."""
        self._api_client = None

    # -- Phase 1: connectivity / auth smoke test ------------------------------
    def test_connection(self) -> ConnectionCheckResult:
        """Calls the User Profile endpoint as a lightweight way to confirm
        the stored token is valid, matches the configured environment
        (sandbox/live), and that the SDK is wired correctly.

        Note: the exact UserApi method name/signature can shift between SDK
        versions — if this raises AttributeError, check the installed
        `upstox-python` version's UserApi against this call.
        """
        try:
            user_api = upstox_client.UserApi(self.api_client)
            response = user_api.get_profile(api_version="2.0")
        except ReauthRequired as exc:
            logger.error("Upstox authentication required: %s", exc)
            return ConnectionCheckResult(ok=False, detail=str(exc))
        except ApiException as exc:
            logger.error("Upstox API error during connection test: %s", exc)
            return ConnectionCheckResult(ok=False, detail=f"API error: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error during connection test: %s", exc)
            return ConnectionCheckResult(
                ok=False,
                detail=(
                    "Unexpected error — verify the installed upstox-python SDK's "
                    f"UserApi.get_profile signature matches this code. Raw error: {exc}"
                ),
            )

        data = getattr(response, "data", None)
        name = getattr(data, "user_name", None) if data else None
        user_id = getattr(data, "user_id", None) if data else None

        logger.info("Upstox connection OK (sandbox=%s), user=%s", self.env.sandbox, name)
        return ConnectionCheckResult(
            ok=True, detail="Authenticated successfully.", user_name=name, user_id=user_id
        )


if __name__ == "__main__":
    from config.config_loader import load_settings

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()

    client = UpstoxRestClient(settings.env)
    result = client.test_connection()
    if result.ok:
        print(f"Connected as {result.user_name} (user_id={result.user_id}, "
              f"sandbox={settings.env.sandbox})")
    else:
        print(f"Connection failed: {result.detail}")
