from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pyotp
import pytest

import webapp.app as app_module
import webapp.credential_vault as cv
import webapp.secrets_bootstrap as sb
import webapp.user_store as us


@pytest.fixture()
def client(tmp_path, monkeypatch):
    sb.SECRET_KEY_PATH = tmp_path / "secret.key"
    sb.ENCRYPTION_KEY_PATH = tmp_path / "encryption.key"
    cv.DB_PATH = tmp_path / "credentials.db"
    us.DB_PATH = tmp_path / "credentials.db"
    app_module.TOKEN_STORE_DIR = tmp_path / "tokens"

    app = app_module.create_app()
    app.testing = True
    return app.test_client()


def _register(client, username="alice", password="password123"):
    return client.post("/register", data={
        "username": username, "password": password, "confirm_password": password,
    }, follow_redirects=False)


def _login(client, username="alice", password="password123"):
    return client.post("/login", data={"username": username, "password": password},
                        follow_redirects=False)


# -- zero-env-setup bootstrap -------------------------------------------------

def test_secrets_auto_generate_with_no_env_vars_set(client, monkeypatch):
    for var in ("WEBAPP_SECRET_KEY", "WEBAPP_ENCRYPTION_KEY", "WEBAPP_ADMIN_USER", "WEBAPP_ADMIN_PASSWORD_HASH"):
        monkeypatch.delenv(var, raising=False)
    # If create_app() required any of these, the fixture itself would have
    # already failed — this test documents the requirement explicitly.
    r = client.get("/register")
    assert r.status_code == 200


def test_secret_and_encryption_keys_persist_across_app_restarts(tmp_path):
    sb.SECRET_KEY_PATH = tmp_path / "secret.key"
    sb.ENCRYPTION_KEY_PATH = tmp_path / "encryption.key"
    cv.DB_PATH = tmp_path / "credentials.db"
    us.DB_PATH = tmp_path / "credentials.db"

    app1 = app_module.create_app()
    key1 = app1.secret_key

    app2 = app_module.create_app()  # simulates a process restart
    key2 = app2.secret_key

    assert key1 == key2  # same key reused, not regenerated (would break decryption otherwise)


# -- registration --------------------------------------------------------

def test_dashboard_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_register_creates_account_and_logs_in(client):
    r = _register(client)
    assert r.status_code == 302
    r = client.get("/")
    assert r.status_code == 200


def test_register_duplicate_username_rejected(client):
    _register(client, "alice")
    client.get("/logout")
    r = _register(client, "alice")
    assert b"already taken" in r.data


def test_register_short_password_rejected(client):
    r = client.post("/register", data={
        "username": "bob", "password": "short", "confirm_password": "short",
    })
    assert b"at least" in r.data.lower()


def test_register_mismatched_passwords_rejected(client):
    r = client.post("/register", data={
        "username": "carol", "password": "password123", "confirm_password": "different456",
    })
    assert b"do not match" in r.data.lower()


# -- login --------------------------------------------------------------

def test_login_with_registered_credentials_succeeds(client):
    _register(client)
    client.get("/logout")
    r = _login(client)
    assert r.status_code == 302


def test_login_wrong_password_rejected(client):
    _register(client)
    client.get("/logout")
    r = client.post("/login", data={"username": "alice", "password": "wrongpassword"})
    assert b"Invalid" in r.data


def test_login_nonexistent_user_rejected(client):
    r = client.post("/login", data={"username": "nobody", "password": "whatever123"})
    assert b"Invalid" in r.data


def test_logout_clears_session(client):
    _register(client)
    client.get("/logout")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


# -- brokers + credentials + connect (unchanged behavior, new auth layer) -----

def test_dashboard_lists_both_brokers(client):
    _register(client)
    r = client.get("/")
    assert b"fyers" in r.data
    assert b"angelone" in r.data
    assert b"Needs credentials" in r.data


def test_saving_fyers_credentials_and_oauth_connect(client):
    _register(client)
    r = client.post("/brokers/fyers/credentials", data={
        "client_id": "XC1234-100", "secret_key": "mysecret",
        "redirect_uri": "https://example.com/brokers/fyers/callback",
    }, follow_redirects=False)
    assert r.status_code == 302

    r = client.get("/brokers/fyers/connect", follow_redirects=False)
    assert r.status_code == 302
    assert "api-t1.fyers.in" in r.headers["Location"]
    assert "client_id=XC1234-100" in r.headers["Location"]


def test_saving_angelone_credentials_and_direct_connect(client):
    _register(client)
    totp_secret = pyotp.random_base32()
    r = client.post("/brokers/angelone/credentials", data={
        "api_key": "test_key", "client_code": "A123456",
        "pin": "1234", "totp_secret": totp_secret,
    }, follow_redirects=False)
    assert r.status_code == 302

    with patch("brokers.angelone_adapter.SmartConnect") as MockSmartConnect:
        MockSmartConnect.return_value.generateSession.return_value = {
            "status": True,
            "data": {
                "jwtToken": "jwt", "refreshToken": "r", "feedToken": "f",
                "name": "Test User", "clientcode": "A123456",
            },
        }
        r = client.get("/brokers/angelone/connect", follow_redirects=False)

    # Direct-credential broker: straight to dashboard, no OAuth URL.
    assert r.status_code == 302
    assert r.headers["Location"] in ("/", "http://localhost/")

    r = client.get("/")
    assert b"Connected" in r.data


def test_credentials_isolated_between_different_registered_users(client):
    _register(client, "alice", "password123")
    client.post("/brokers/fyers/credentials", data={
        "client_id": "ALICE_ID", "secret_key": "s", "redirect_uri": "https://x.com",
    })
    client.get("/logout")

    _register(client, "bob", "password456")
    r = client.get("/brokers/fyers/credentials")
    assert b"ALICE_ID" not in r.data  # bob doesn't see alice's saved client_id


def test_unknown_broker_returns_404(client):
    _register(client)
    r = client.get("/brokers/nonexistent-broker/credentials")
    assert r.status_code == 404


def test_connect_without_credentials_redirects_to_credentials_form(client):
    _register(client)
    r = client.get("/brokers/fyers/connect", follow_redirects=False)
    assert r.status_code == 302
    assert "/brokers/fyers/credentials" in r.headers["Location"]


# -- trading engine merged into this process ----------------------------------

def test_trading_status_reports_not_running_before_start(client):
    _register(client)
    r = client.get("/brokers/fyers/trading_status")
    assert r.status_code == 200
    assert r.get_json() == {"running": False}


def test_start_trading_without_connection_redirects_to_credentials(client):
    _register(client)
    r = client.post("/brokers/fyers/start_trading", follow_redirects=False)
    assert r.status_code == 302
    assert "/brokers/fyers/credentials" in r.headers["Location"]


def test_start_trading_launches_a_real_session_visible_in_status(client):
    from unittest.mock import MagicMock, patch

    _register(client)
    client.post("/brokers/fyers/credentials", data={
        "client_id": "XC1234-100", "secret_key": "mysecret", "redirect_uri": "https://example.com/cb",
    })
    # Manually mark connected (normally set by the OAuth callback route).
    import webapp.credential_vault as cv
    vault = cv.CredentialVault(app_module.get_or_create_encryption_key(), db_path=cv.DB_PATH)
    vault.set_connected("alice", "fyers", True)

    fake_settings = MagicMock()
    fake_settings.raw = {"app": {"environment": "paper"}}
    fake_settings.env.paper_mode = True

    with patch("orchestration.session_manager.load_settings", return_value=fake_settings), \
         patch("orchestration.session_manager.build_connected_adapter") as mock_build, \
         patch("orchestration.session_manager.TradingSession") as MockTradingSession:
        mock_build.return_value = MagicMock()
        mock_session = MagicMock()
        mock_session.get_status.return_value = {
            "open_positions": [], "daily_trades": 0, "daily_realized_pnl_inr": 0.0,
            "can_trade": True, "guard_reason": "OK", "feed_open": True,
        }
        MockTradingSession.return_value = mock_session

        r = client.post("/brokers/fyers/start_trading", follow_redirects=False)
        assert r.status_code == 302

        status_r = client.get("/brokers/fyers/trading_status")
        status = status_r.get_json()
        assert status["running"] is True
        assert status["daily_trades"] == 0

        stop_r = client.post("/brokers/fyers/stop_trading", follow_redirects=False)
        assert stop_r.status_code == 302
        mock_session.stop.assert_called_once()

        status_after_stop = client.get("/brokers/fyers/trading_status").get_json()
        assert status_after_stop == {"running": False}


def test_disconnect_while_trading_stops_the_session_first(client):
    from unittest.mock import MagicMock, patch

    _register(client)
    client.post("/brokers/fyers/credentials", data={
        "client_id": "XC1234-100", "secret_key": "mysecret", "redirect_uri": "https://example.com/cb",
    })
    import webapp.credential_vault as cv
    vault = cv.CredentialVault(app_module.get_or_create_encryption_key(), db_path=cv.DB_PATH)
    vault.set_connected("alice", "fyers", True)

    fake_settings = MagicMock()
    fake_settings.raw = {"app": {"environment": "paper"}}
    fake_settings.env.paper_mode = True

    with patch("orchestration.session_manager.load_settings", return_value=fake_settings), \
         patch("orchestration.session_manager.build_connected_adapter") as mock_build, \
         patch("orchestration.session_manager.TradingSession") as MockTradingSession:
        mock_build.return_value = MagicMock()
        mock_session = MagicMock()
        MockTradingSession.return_value = mock_session

        client.post("/brokers/fyers/start_trading")
        client.get("/brokers/fyers/disconnect", follow_redirects=False)

        mock_session.stop.assert_called_once()  # disconnect stopped the running session, not just left it dangling


# -- log viewer ------------------------------------------------------------

def test_logs_page_requires_login(client):
    r = client.get("/logs", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_logs_page_loads_when_logged_in(client):
    _register(client)
    r = client.get("/logs")
    assert r.status_code == 200


def test_logs_tail_returns_json_with_placeholder_when_no_log_file(client, tmp_path):
    from unittest.mock import patch
    _register(client)
    with patch("config.logging_setup.DEFAULT_LOG_DIR", tmp_path / "nonexistent_logs"):
        r = client.get("/logs/tail")
    assert r.status_code == 200
    data = r.get_json()
    assert "lines" in data


def test_logs_tail_returns_actual_log_content(client, tmp_path):
    from unittest.mock import patch
    _register(client)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "bot.log").write_text("line1\nline2\nline3\n", encoding="utf-8")

    with patch("config.logging_setup.DEFAULT_LOG_DIR", log_dir):
        r = client.get("/logs/tail")
    data = r.get_json()
    assert data["lines"] == ["line1", "line2", "line3"]


def test_status_route_throttles_repeated_polls_to_avoid_hammering_the_broker(client):
    """Regression test for a real bug found live: the dashboard polls
    /brokers/<name>/status every 8s, and each call used to hit the real
    broker's connectivity check (and for AngelOne, could cascade into a
    full re-login). 5 rapid polls must collapse into 1 real broker call."""
    import pyotp
    from unittest.mock import patch

    _register(client)
    totp_secret = pyotp.random_base32()
    client.post("/brokers/angelone/credentials", data={
        "api_key": "test_key", "client_code": "A123456", "pin": "1234", "totp_secret": totp_secret,
    })

    call_count = {"n": 0}

    def fake_test_connection(self):
        call_count["n"] += 1
        from brokers.base import ConnectionCheckResult
        return ConnectionCheckResult(ok=True, detail="ok", user_name="Test", user_id="A123456")

    with patch("brokers.angelone_adapter.AngelOneBrokerAdapter.test_connection", fake_test_connection), \
         patch("brokers.angelone_adapter.AngelOneBrokerAdapter.is_authenticated", return_value=True):
        for _ in range(5):
            client.get("/brokers/angelone/status")

    assert call_count["n"] == 1


def test_connect_invalidates_stale_cached_status(client):
    """Regression test for a real bug seen live: the status-throttle cache
    (from the previous fix) wasn't cleared when the user clicked Connect,
    so right after a successful reconnect the dashboard kept showing the
    OLD cached 'invalid token' result for up to 45 seconds. Connect,
    Disconnect, and the OAuth callback must all invalidate the cache for
    that (user, broker) so status reflects reality immediately after an
    action, not just on the next throttle window."""
    import pyotp
    from unittest.mock import patch
    from brokers.base import ConnectionCheckResult

    _register(client)
    totp_secret = pyotp.random_base32()
    client.post("/brokers/angelone/credentials", data={
        "api_key": "test_key", "client_code": "A123456", "pin": "1234", "totp_secret": totp_secret,
    })

    results = [ConnectionCheckResult(ok=False, detail="invalid token", user_name="Test", user_id="A123456"),
               ConnectionCheckResult(ok=True, detail="ok", user_name="Test", user_id="A123456")]

    def fake_test_connection(self):
        return results.pop(0) if results else ConnectionCheckResult(ok=True, detail="ok")

    with patch("brokers.angelone_adapter.SmartConnect") as MockSmartConnect, \
         patch("brokers.angelone_adapter.AngelOneBrokerAdapter.test_connection", fake_test_connection), \
         patch("brokers.angelone_adapter.AngelOneBrokerAdapter.is_authenticated", return_value=True):
        MockSmartConnect.return_value.generateSession.return_value = {
            "status": True,
            "data": {"jwtToken": "j", "refreshToken": "r", "feedToken": "f", "name": "Test", "clientcode": "A123456"},
        }

        r1 = client.get("/brokers/angelone/status").get_json()
        assert r1["connected"] is False  # cached now

        client.get("/brokers/angelone/connect")  # fresh login — must invalidate the cache

        r2 = client.get("/brokers/angelone/status").get_json()
        assert r2["connected"] is True  # must NOT be the stale cached "False" result
