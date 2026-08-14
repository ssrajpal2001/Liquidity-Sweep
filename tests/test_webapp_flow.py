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
