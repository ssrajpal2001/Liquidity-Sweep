"""
webapp/broker_session_builder.py

Shared helper: given a username and broker name, loads that client's
saved credentials from the vault and returns a connected BrokerAdapter.
Factored out here so backtest/run_backtest.py, orchestration/
session_manager.py, and any future diagnostic script all share one
implementation instead of three independently drifting copies of the
same vault-lookup + connect logic.
"""
from __future__ import annotations

import logging

from brokers.base import AuthType, BrokerAdapter
from brokers.registry import get_adapter_class
from webapp.credential_vault import CredentialVault
from webapp.secrets_bootstrap import get_or_create_encryption_key

logger = logging.getLogger(__name__)

# Import lazily inside the function below to avoid a circular import
# (webapp.app imports things that, transitively, would import this
# module if it were imported at module load time here).


class BrokerSessionError(RuntimeError):
    pass


def build_connected_adapter(username: str, broker_name: str, paper_mode: bool = True) -> BrokerAdapter:
    from webapp.app import TOKEN_STORE_DIR, _build_env_like

    vault = CredentialVault(get_or_create_encryption_key())
    fields = vault.get_credentials(username, broker_name)
    if fields is None:
        raise BrokerSessionError(f"No {broker_name} credentials saved for '{username}'.")

    env_like = _build_env_like(fields, TOKEN_STORE_DIR / f"{username}__{broker_name}_token_store.json")
    adapter = get_adapter_class(broker_name)(env_like, paper_mode=paper_mode)

    if adapter.auth_type == AuthType.DIRECT_CREDENTIALS:
        check = adapter.login()  # safe to re-run — no browser round trip (e.g. AngelOne's TOTP)
        if not check.ok:
            raise BrokerSessionError(f"{broker_name} login failed for '{username}': {check.detail}")
    elif not adapter.is_authenticated():
        raise BrokerSessionError(
            f"'{username}' has saved {broker_name} credentials but isn't connected — Connect via the web UI first."
        )

    logger.info("[BROKER_SESSION_READY] user=%s broker=%s", username, broker_name)
    return adapter
