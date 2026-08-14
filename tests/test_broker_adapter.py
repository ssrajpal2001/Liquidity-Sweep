from __future__ import annotations

from unittest.mock import MagicMock

from brokers.base import BrokerAdapter
from brokers.fyers_adapter import FyersBrokerAdapter
from brokers.registry import available_brokers, get_adapter_class


class _FakeEnv:
    client_id = "XC1234-100"
    secret_key = "fake_secret"
    redirect_uri = "https://example.com/callback"
    paper_mode = True
    auth_code = None
    token_store_path = None  # overridden per-test via tmp_path


def _make_env(tmp_path):
    env = _FakeEnv()
    env.token_store_path = tmp_path / "token_store.json"
    return env


def test_registry_lists_fyers():
    assert "fyers" in available_brokers()


def test_get_adapter_class_returns_fyers_adapter():
    assert get_adapter_class("fyers") is FyersBrokerAdapter


def test_unknown_broker_raises_clear_error():
    import pytest
    with pytest.raises(ValueError, match="Unknown broker"):
        get_adapter_class("some_broker_that_does_not_exist")


def test_adapter_is_a_broker_adapter():
    """The whole point of the interface: isinstance check passes, which is
    what lets main.py (or a future session manager) hold a `BrokerAdapter`
    typed variable and never know it's specifically Fyers underneath."""
    from config.config_loader import EnvConfig
    from pathlib import Path

    env = EnvConfig(
        client_id="XC1234-100", secret_key="fake", redirect_uri="https://example.com",
        paper_mode=True, auth_code=None, token_store_path=Path("/tmp/does_not_matter.json"),
    )
    adapter = FyersBrokerAdapter(env, paper_mode=True)
    assert isinstance(adapter, BrokerAdapter)
    assert adapter.broker_name == "fyers"


def test_adapter_construction_never_requires_a_token(tmp_path):
    """Regression test for a real bug caught during integration testing:
    the adapter used to eagerly build the authenticated Fyers model in
    __init__ (via ExpiryResolver/OrderManager), which crashed with
    ReauthRequired before login had even happened. Adapter construction
    must be fully lazy — nothing should touch the network or require a
    token until the caller actually starts using it."""
    from config.config_loader import EnvConfig

    env = EnvConfig(
        client_id="XC1234-100", secret_key="fake", redirect_uri="https://example.com",
        paper_mode=True, auth_code=None, token_store_path=tmp_path / "token_store.json",
    )
    adapter = FyersBrokerAdapter(env, paper_mode=True)  # must not raise
    assert adapter.is_authenticated() is False
    assert adapter.is_feed_open is False
    assert adapter.seconds_since_last_message() is None


def test_adapter_login_url_and_auth_delegate_correctly(tmp_path):
    from config.config_loader import EnvConfig

    env = EnvConfig(
        client_id="XC1234-100", secret_key="fake", redirect_uri="https://example.com/cb",
        paper_mode=True, auth_code=None, token_store_path=tmp_path / "token_store.json",
    )
    adapter = FyersBrokerAdapter(env, paper_mode=True)
    url = adapter.build_login_url()
    assert "client_id=XC1234-100" in url
    assert "generate-authcode" in url
