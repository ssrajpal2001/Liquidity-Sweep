from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pyotp
import pytest

import webapp.app as app_module
import webapp.credential_vault as cv
import webapp.secrets_bootstrap as sb
import webapp.user_store as us
from orchestration.session_manager import RunningSession, SessionManager, discover_connected_pairs
from webapp.credential_vault import CredentialVault
from webapp.secrets_bootstrap import get_or_create_encryption_key
from webapp.user_store import UserStore


@pytest.fixture()
def vault_setup(tmp_path, monkeypatch):
    sb.SECRET_KEY_PATH = tmp_path / "secret.key"
    sb.ENCRYPTION_KEY_PATH = tmp_path / "encryption.key"
    cv.DB_PATH = tmp_path / "credentials.db"
    us.DB_PATH = tmp_path / "credentials.db"
    app_module.TOKEN_STORE_DIR = tmp_path / "tokens"

    users = UserStore()
    users.register("alice", "password123", "password123")
    users.register("bob", "password456", "password456")

    vault = CredentialVault(get_or_create_encryption_key())
    vault.save_credentials("alice", "fyers", {"client_id": "XC1-100", "secret_key": "s", "redirect_uri": "https://x"})
    vault.set_connected("alice", "fyers", True)

    vault.save_credentials("bob", "angelone", {
        "api_key": "k", "client_code": "B1", "pin": "1234", "totp_secret": pyotp.random_base32(),
    })
    vault.set_connected("bob", "angelone", True)

    # bob also has Fyers credentials saved but NOT connected — must be excluded.
    vault.save_credentials("bob", "fyers", {"client_id": "XC2-100", "secret_key": "s2", "redirect_uri": "https://y"})

    return tmp_path


def test_discovery_finds_only_connected_pairs(vault_setup):
    pairs = discover_connected_pairs()
    assert ("alice", "fyers") in pairs
    assert ("bob", "angelone") in pairs
    assert ("bob", "fyers") not in pairs  # saved but not connected
    assert len(pairs) == 2


def test_discovery_finds_nothing_when_no_one_connected(tmp_path, monkeypatch):
    sb.SECRET_KEY_PATH = tmp_path / "secret.key"
    sb.ENCRYPTION_KEY_PATH = tmp_path / "encryption.key"
    cv.DB_PATH = tmp_path / "credentials.db"
    us.DB_PATH = tmp_path / "credentials.db"

    UserStore().register("carol", "password789", "password789")
    # no credentials saved at all
    assert discover_connected_pairs() == []


def test_session_manager_starts_independent_session_per_pair(vault_setup):
    manager = SessionManager()

    fake_settings = MagicMock()
    fake_settings.raw = {"app": {"environment": "paper"}}
    fake_settings.env.paper_mode = True

    with patch("orchestration.session_manager.load_settings", return_value=fake_settings), \
         patch("orchestration.session_manager.build_connected_adapter") as mock_build, \
         patch("orchestration.session_manager.TradingSession") as MockTradingSession:
        mock_build.return_value = MagicMock()
        MockTradingSession.return_value = MagicMock()

        manager.sync()

    assert ("alice", "fyers") in manager.running
    assert ("bob", "angelone") in manager.running
    assert len(manager.running) == 2

    # Each pair got its OWN TradingSession instance and its own session_id —
    # this is the actual isolation guarantee (separate state file per pair).
    call_session_ids = [call.kwargs.get("session_id") for call in MockTradingSession.call_args_list]
    assert "alice__fyers" in call_session_ids
    assert "bob__angelone" in call_session_ids


def test_session_manager_does_not_double_start_same_pair(vault_setup):
    manager = SessionManager()
    fake_settings = MagicMock()
    fake_settings.raw = {"app": {"environment": "paper"}}
    fake_settings.env.paper_mode = True

    with patch("orchestration.session_manager.load_settings", return_value=fake_settings), \
         patch("orchestration.session_manager.build_connected_adapter") as mock_build, \
         patch("orchestration.session_manager.TradingSession") as MockTradingSession:
        mock_build.return_value = MagicMock()
        MockTradingSession.return_value = MagicMock()

        manager.sync()
        manager.sync()  # run again — should NOT start duplicate sessions

    assert MockTradingSession.call_count == 2  # still just 2, not 4


def test_session_manager_stop_all_stops_every_running_session(vault_setup):
    manager = SessionManager()
    fake_settings = MagicMock()
    fake_settings.raw = {"app": {"environment": "paper"}}
    fake_settings.env.paper_mode = True

    with patch("orchestration.session_manager.load_settings", return_value=fake_settings), \
         patch("orchestration.session_manager.build_connected_adapter") as mock_build, \
         patch("orchestration.session_manager.TradingSession") as MockTradingSession:
        mock_build.return_value = MagicMock()
        session_mocks = [MagicMock(), MagicMock()]
        MockTradingSession.side_effect = session_mocks

        manager.sync()
        manager.stop_all()

    for mock_session in session_mocks:
        mock_session.stop.assert_called_once()
    assert manager.running == {}


def test_session_manager_one_broken_session_does_not_block_others(vault_setup):
    """A build_connected_adapter failure for one (user, broker) pair must
    not prevent the other pairs from starting."""
    from webapp.broker_session_builder import BrokerSessionError

    manager = SessionManager()
    fake_settings = MagicMock()
    fake_settings.raw = {"app": {"environment": "paper"}}
    fake_settings.env.paper_mode = True

    def flaky_build(username, broker, paper_mode=True):
        if username == "alice":
            raise BrokerSessionError("token expired")
        return MagicMock()

    with patch("orchestration.session_manager.load_settings", return_value=fake_settings), \
         patch("orchestration.session_manager.build_connected_adapter", side_effect=flaky_build), \
         patch("orchestration.session_manager.TradingSession") as MockTradingSession:
        MockTradingSession.return_value = MagicMock()
        manager.sync()

    assert ("alice", "fyers") not in manager.running  # failed, correctly excluded
    assert ("bob", "angelone") in manager.running     # unaffected by alice's failure
