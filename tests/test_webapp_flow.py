from __future__ import annotations

import pytest
from werkzeug.security import generate_password_hash

import webapp.app as app_module
import webapp.credential_vault as cv


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBAPP_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WEBAPP_ENCRYPTION_KEY", cv.generate_encryption_key())
    monkeypatch.setenv("WEBAPP_ADMIN_USER", "admin")
    monkeypatch.setenv("WEBAPP_ADMIN_PASSWORD_HASH", generate_password_hash("testpass123"))

    cv.DB_PATH = tmp_path / "credentials.db"
    app_module.TOKEN_STORE_DIR = tmp_path / "tokens"

    app = app_module.create_app()
    app.testing = True
    return app.test_client()


def _login(client):
    return client.post("/login", data={"username": "admin", "password": "testpass123"},
                        follow_redirects=False)


def test_dashboard_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_wrong_password_rejected(client):
    r = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert b"Invalid" in r.data


def test_correct_login_redirects_to_dashboard(client):
    r = _login(client)
    assert r.status_code == 302
    assert r.headers["Location"] in ("/", "http://localhost/")


def test_dashboard_lists_fyers_with_no_credentials_initially(client):
    _login(client)
    r = client.get("/")
    assert r.status_code == 200
    assert b"fyers" in r.data
    assert b"Needs credentials" in r.data


def test_saving_credentials_updates_dashboard_status(client):
    _login(client)
    r = client.post("/brokers/fyers/credentials", data={
        "client_id": "XC1234-100", "secret_key": "mysecret",
        "redirect_uri": "https://example.com/brokers/fyers/callback",
    }, follow_redirects=False)
    assert r.status_code == 302

    r = client.get("/")
    assert b"Saved" in r.data
    assert b"Connect" in r.data


def test_connect_redirects_to_real_broker_oauth_url_built_from_saved_creds(client):
    _login(client)
    client.post("/brokers/fyers/credentials", data={
        "client_id": "XC1234-100", "secret_key": "mysecret",
        "redirect_uri": "https://example.com/brokers/fyers/callback",
    })
    r = client.get("/brokers/fyers/connect", follow_redirects=False)
    assert r.status_code == 302
    location = r.headers["Location"]
    assert "api-t1.fyers.in" in location
    assert "client_id=XC1234-100" in location
    assert "generate-authcode" in location


def test_connect_without_credentials_redirects_to_credentials_form(client):
    _login(client)
    r = client.get("/brokers/fyers/connect", follow_redirects=False)
    assert r.status_code == 302
    assert "/brokers/fyers/credentials" in r.headers["Location"]


def test_unknown_broker_returns_404(client):
    _login(client)
    r = client.get("/brokers/nonexistent-broker/credentials")
    assert r.status_code == 404


def test_editing_credentials_with_blank_secret_keeps_existing_value(client):
    _login(client)
    client.post("/brokers/fyers/credentials", data={
        "client_id": "XC1234-100", "secret_key": "original-secret",
        "redirect_uri": "https://example.com/cb",
    })
    # Re-save with secret_key left blank (simulating the "leave blank to keep current" form behavior)
    client.post("/brokers/fyers/credentials", data={
        "client_id": "XC9999-100", "secret_key": "", "redirect_uri": "https://example.com/cb-new",
    })

    from config.config_loader import PROJECT_ROOT  # noqa: F401 -- ensures app context available
    v = cv.CredentialVault(app_module.os.environ["WEBAPP_ENCRYPTION_KEY"], db_path=cv.DB_PATH)
    creds = v.get_credentials("admin", "fyers")
    assert creds["client_id"] == "XC9999-100"       # updated
    assert creds["secret_key"] == "original-secret"  # preserved, not wiped by blank submission


def test_logout_clears_session(client):
    _login(client)
    client.get("/logout")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_angelone_connect_uses_direct_login_not_oauth_redirect(client, monkeypatch):
    """Regression test for a real bug: _build_env_like() was hardcoded to
    Fyers' field names (client_id/secret_key/redirect_uri), so AngelOne's
    different fields (api_key/client_code/pin/totp_secret) silently
    weren't passed through, breaking login with an AttributeError. This
    proves a second broker with a completely different credential shape
    and a completely different auth mechanism (direct TOTP login, no
    browser redirect) works through the same generic UI code."""
    import pyotp
    from unittest.mock import patch

    _login(client)
    totp_secret = pyotp.random_base32()
    client.post("/brokers/angelone/credentials", data={
        "api_key": "test_key", "client_code": "A123456",
        "pin": "1234", "totp_secret": totp_secret,
    })

    with patch("brokers.angelone_adapter.SmartConnect") as MockSmartConnect:
        MockSmartConnect.return_value.generateSession.return_value = {
            "status": True,
            "data": {
                "jwtToken": "jwt", "refreshToken": "r", "feedToken": "f",
                "name": "Test User", "clientcode": "A123456",
            },
        }
        r = client.get("/brokers/angelone/connect", follow_redirects=False)

    # Direct-credential brokers go straight to the dashboard — no OAuth
    # provider URL in the redirect, unlike Fyers' /brokers/fyers/connect.
    assert r.status_code == 302
    assert r.headers["Location"] in ("/", "http://localhost/")

    r = client.get("/")
    assert b"Connected" in r.data


def test_both_fyers_and_angelone_appear_in_broker_list(client):
    _login(client)
    r = client.get("/")
    assert b"fyers" in r.data
    assert b"angelone" in r.data
