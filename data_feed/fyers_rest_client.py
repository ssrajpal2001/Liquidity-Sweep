"""
data_feed/fyers_rest_client.py

Thin wrapper around the official `fyers-apiv3` SDK's FyersModel — the
Fyers equivalent of what upstox_rest_client.py was for Upstox. Builds one
authenticated FyersModel and exposes test_connection() to confirm the
stored token is actually accepted before the bot is trusted to run.

Per auth/auth.py's docstring: Fyers has no clock-based expiry guarantee
as clean as Upstox's, so THIS live check — not a stored timestamp — is
the authoritative signal that today's token still works.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from fyers_apiv3 import fyersModel

from auth.auth import FyersAuth, ReauthRequired

logger = logging.getLogger(__name__)


@dataclass
class ConnectionCheckResult:
    ok: bool
    detail: str
    user_name: Optional[str] = None
    user_id: Optional[str] = None


class FyersRestClient:
    """Owns one authenticated FyersModel and hands it to whichever
    higher-level module (option selector, order manager, expiry resolver)
    needs to make a REST call."""

    def __init__(self, env, auth: Optional[FyersAuth] = None):
        # `env` is a config.config_loader.EnvConfig.
        self.env = env
        self.auth = auth or FyersAuth(env)
        self._model: Optional["fyersModel.FyersModel"] = None

    def _build_model(self) -> "fyersModel.FyersModel":
        record = self.auth.get_valid_token_record()  # raises ReauthRequired if none/soft-expired
        return fyersModel.FyersModel(
            client_id=record.client_id,
            token=record.access_token,
            is_async=False,
            log_path="",
        )

    @property
    def model(self) -> "fyersModel.FyersModel":
        if self._model is None:
            self._model = self._build_model()
        return self._model

    def refresh_client(self) -> None:
        """Force the next `model` access to rebuild from the current
        stored token — call this right after a fresh login/token exchange."""
        self._model = None

    # -- Phase 1-equivalent: connectivity / auth smoke test --------------------
    def test_connection(self) -> ConnectionCheckResult:
        """Calls get_profile() as a lightweight way to confirm the stored
        token is actually accepted right now."""
        try:
            response = self.model.get_profile()
        except ReauthRequired as exc:
            logger.error("Fyers authentication required: %s", exc)
            return ConnectionCheckResult(ok=False, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error during connection test.")
            return ConnectionCheckResult(ok=False, detail=f"Unexpected error: {exc}")

        if not isinstance(response, dict) or response.get("s") != "ok":
            logger.error("Fyers profile check failed: %s", response)
            return ConnectionCheckResult(ok=False, detail=f"API error: {response}")

        data = response.get("data", {}) or {}
        name = data.get("name")
        user_id = data.get("fy_id")
        logger.info("Fyers connection OK (paper_mode=%s), user=%s", self.env.paper_mode, name)
        return ConnectionCheckResult(ok=True, detail="Authenticated successfully.",
                                      user_name=name, user_id=user_id)


if __name__ == "__main__":
    from config.config_loader import load_settings

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()

    client = FyersRestClient(settings.env)
    result = client.test_connection()
    if result.ok:
        print(f"Connected as {result.user_name} (user_id={result.user_id}, "
              f"paper_mode={settings.env.paper_mode})")
    else:
        print(f"Connection failed: {result.detail}")
