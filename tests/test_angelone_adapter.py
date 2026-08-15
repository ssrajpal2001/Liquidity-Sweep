from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pyotp
import pytest

from brokers.angelone_adapter import AngelOneBrokerAdapter, AngelOneSession, AngelOneSessionStore
from brokers.base import AuthType, BrokerAdapter


def _make_env(tmp_path):
    return SimpleNamespace(
        api_key="test_api_key",
        client_code="A123456",
        pin="1234",
        totp_secret=pyotp.random_base32(),
        token_store_path=tmp_path / "angelone_session.json",
    )


def test_adapter_is_a_broker_adapter(tmp_path):
    adapter = AngelOneBrokerAdapter(_make_env(tmp_path), paper_mode=True)
    assert isinstance(adapter, BrokerAdapter)
    assert adapter.broker_name == "angelone"


def test_auth_type_is_direct_credentials(tmp_path):
    adapter = AngelOneBrokerAdapter(_make_env(tmp_path), paper_mode=True)
    assert adapter.auth_type == AuthType.DIRECT_CREDENTIALS


def test_oauth_methods_raise_not_implemented(tmp_path):
    """AngelOne doesn't use OAuth redirect — calling those methods should
    fail clearly, not silently do the wrong thing."""
    adapter = AngelOneBrokerAdapter(_make_env(tmp_path), paper_mode=True)
    with pytest.raises(NotImplementedError):
        adapter.build_login_url()
    with pytest.raises(NotImplementedError):
        adapter.exchange_code("whatever")


def test_construction_never_requires_network(tmp_path):
    # Must not raise, must not touch the network.
    adapter = AngelOneBrokerAdapter(_make_env(tmp_path), paper_mode=True)
    assert adapter.is_authenticated() is False


def test_login_success_persists_session(tmp_path):
    env = _make_env(tmp_path)
    adapter = AngelOneBrokerAdapter(env, paper_mode=True)

    fake_response = {
        "status": True,
        "data": {
            "jwtToken": "fake.jwt.token",
            "refreshToken": "fake_refresh",
            "feedToken": "fake_feed",
            "name": "Test User",
            "clientcode": env.client_code,
        },
    }
    with patch("brokers.angelone_adapter.SmartConnect") as MockSmartConnect:
        MockSmartConnect.return_value.generateSession.return_value = fake_response
        result = adapter.login()

    assert result.ok is True
    assert result.user_name == "Test User"

    session = adapter.session_store.load()
    assert session is not None
    assert session.jwt_token == "fake.jwt.token"
    assert session.client_code == env.client_code


def test_login_failure_does_not_persist_session(tmp_path):
    env = _make_env(tmp_path)
    adapter = AngelOneBrokerAdapter(env, paper_mode=True)

    with patch("brokers.angelone_adapter.SmartConnect") as MockSmartConnect:
        MockSmartConnect.return_value.generateSession.return_value = {
            "status": False, "message": "Invalid totp",
        }
        result = adapter.login()

    assert result.ok is False
    assert "Invalid totp" in result.detail
    assert adapter.session_store.load() is None


def test_login_uses_real_totp_generation(tmp_path):
    """Confirms pyotp is actually invoked with the stored secret, not a
    hardcoded/placeholder value."""
    env = _make_env(tmp_path)
    adapter = AngelOneBrokerAdapter(env, paper_mode=True)

    captured = {}
    with patch("brokers.angelone_adapter.SmartConnect") as MockSmartConnect:
        def fake_generate_session(client_code, pin, totp):
            captured["totp"] = totp
            return {"status": False, "message": "irrelevant for this test"}
        MockSmartConnect.return_value.generateSession.side_effect = fake_generate_session
        adapter.login()

    expected_totp = pyotp.TOTP(env.totp_secret).now()
    assert captured["totp"] == expected_totp
    assert len(captured["totp"]) == 6
    assert captured["totp"].isdigit()


def test_session_store_round_trip(tmp_path):
    store = AngelOneSessionStore(tmp_path / "session.json")
    assert store.load() is None

    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    session = AngelOneSession(
        jwt_token="jwt", refresh_token="refresh", feed_token="feed",
        client_code="A123", generated_at=datetime.now(IST).isoformat(),
    )
    store.save(session)
    loaded = store.load()
    assert loaded.jwt_token == "jwt"
    assert loaded.is_stale() is False

    store.clear()
    assert store.load() is None


def test_session_is_stale_after_max_age():
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    old_session = AngelOneSession(
        jwt_token="jwt", refresh_token="r", feed_token="f", client_code="A123",
        generated_at=(datetime.now(IST) - timedelta(hours=25)).isoformat(),
    )
    assert old_session.is_stale() is True


def test_paper_order_never_calls_real_place_order(tmp_path):
    adapter = AngelOneBrokerAdapter(_make_env(tmp_path), paper_mode=True)
    result = adapter.place_entry_buy("NIFTY25AUG25000CE", 75, 150.0, tag="test")
    assert result.ok is True
    assert result.order_id.startswith("PAPER-ANGELONE-")

    status = adapter.get_order_status(result.order_id)
    assert status["status"] == "complete"


def test_test_connection_does_not_call_login_when_getprofile_fails(tmp_path):
    """Regression test for a real bug found live: test_connection() used
    to silently fall back to a fresh TOTP login() on ANY getProfile
    failure. Since this method gets called by the dashboard's status
    polling every few seconds, that meant continuous re-authentication
    against AngelOne's real servers. It must now just report the
    failure, never call login() as a side effect."""
    from unittest.mock import patch

    adapter = AngelOneBrokerAdapter(_make_env(tmp_path), paper_mode=True)
    mock_smart = MagicMock()
    mock_smart.getProfile.side_effect = RuntimeError("network hiccup")
    adapter._smart = mock_smart

    with patch.object(adapter, "login") as mock_login:
        result = adapter.test_connection()
        mock_login.assert_not_called()

    assert result.ok is False
    assert "Connectivity check failed" in result.detail


def test_test_connection_does_not_call_login_when_profile_status_false(tmp_path):
    from unittest.mock import patch

    adapter = AngelOneBrokerAdapter(_make_env(tmp_path), paper_mode=True)
    mock_smart = MagicMock()
    mock_smart.getProfile.return_value = {"status": False, "message": "session expired"}
    adapter._smart = mock_smart
    adapter.session_store.save = MagicMock()  # avoid touching disk for this test

    with patch.object(adapter, "login") as mock_login:
        result = adapter.test_connection()
        mock_login.assert_not_called()

    assert result.ok is False


def test_test_connection_reports_ok_on_valid_profile(tmp_path):
    adapter = AngelOneBrokerAdapter(_make_env(tmp_path), paper_mode=True)
    mock_smart = MagicMock()
    mock_smart.getProfile.return_value = {
        "status": True, "data": {"name": "Test User", "clientcode": "A123456"},
    }
    adapter._smart = mock_smart

    result = adapter.test_connection()
    assert result.ok is True
    assert result.user_name == "Test User"


def test_test_connection_reports_no_session_without_calling_login(tmp_path):
    from unittest.mock import patch

    adapter = AngelOneBrokerAdapter(_make_env(tmp_path), paper_mode=True)
    # no session saved, no _smart set — _get_smart() will return None

    with patch.object(adapter, "login") as mock_login:
        result = adapter.test_connection()
        mock_login.assert_not_called()

    assert result.ok is False
    assert "Connect" in result.detail


def test_build_token_list_groups_by_exchange():
    result = AngelOneBrokerAdapter._build_token_list(["2:26009", "2:26000", "4:12345"])
    by_exchange = {row["exchangeType"]: sorted(row["tokens"]) for row in result}
    assert by_exchange[2] == ["26000", "26009"]
    assert by_exchange[4] == ["12345"]


def test_tick_handler_parses_valid_message_and_converts_paise_to_rupees(tmp_path):
    adapter = AngelOneBrokerAdapter(_make_env(tmp_path), paper_mode=True)
    received = []
    adapter._on_tick = lambda token, ltp: received.append((token, ltp))

    adapter._handle_data(None, {"token": "26009", "last_traded_price": 2500000})  # paise
    assert received == [("26009", 25000.0)]


def test_tick_handler_ignores_malformed_message_without_crashing(tmp_path, caplog):
    adapter = AngelOneBrokerAdapter(_make_env(tmp_path), paper_mode=True)
    received = []
    adapter._on_tick = lambda token, ltp: received.append((token, ltp))

    adapter._handle_data(None, {"unexpected": "shape"})  # should not raise
    assert received == []


def test_required_credential_fields_include_totp_secret():
    fields = AngelOneBrokerAdapter.required_credential_fields()
    field_names = [f[0] for f in fields]
    assert "totp_secret" in field_names
    assert "pin" in field_names
    assert "client_code" in field_names
    assert "api_key" in field_names


# -- instrument-master-backed methods --------------------------------------

SAMPLE_SCRIP_ROWS = [
    {"token": "58784", "symbol": "NIFTY28OCT2524400CE", "name": "NIFTY", "expiry": "28OCT2025",
     "strike": "2440000.000000", "lotsize": "75", "instrumenttype": "OPTIDX", "exch_seg": "NFO",
     "tick_size": "5.000000"},
    {"token": "58785", "symbol": "NIFTY28OCT2524400PE", "name": "NIFTY", "expiry": "28OCT2025",
     "strike": "2440000.000000", "lotsize": "75", "instrumenttype": "OPTIDX", "exch_seg": "NFO",
     "tick_size": "5.000000"},
]


def _adapter_with_seeded_master(tmp_path):
    from execution.angelone_instrument_master import AngelOneInstrumentMaster
    import json

    cache_path = tmp_path / "scrip_cache.json"
    cache_path.write_text(json.dumps(SAMPLE_SCRIP_ROWS))
    adapter = AngelOneBrokerAdapter(_make_env(tmp_path), paper_mode=True)
    adapter._instrument_master = AngelOneInstrumentMaster(cache_path=cache_path)
    return adapter


def test_nearest_expiry_uses_instrument_master(tmp_path):
    from datetime import date
    adapter = _adapter_with_seeded_master(tmp_path)

    with patch("execution.angelone_instrument_master.date") as mock_date:
        mock_date.today.return_value = date(2025, 10, 1)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        expiry = adapter.nearest_expiry("NIFTY")

    assert expiry == "28OCT2025"  # AngelOne's own DDMMMYYYY format


def test_get_option_chain_returns_legs_with_correct_strikes(tmp_path):
    adapter = _adapter_with_seeded_master(tmp_path)
    adapter._smart = MagicMock()
    adapter._smart.ltpData.return_value = {"data": {"ltp": 150.5}}

    legs = adapter.get_option_chain("NIFTY", "28OCT2025")

    assert len(legs) == 2
    ce_leg = next(l for l in legs if l.option_type == "CE")
    assert ce_leg.strike_price == 24400.0  # not 2440000 — the x100 fix carried through
    assert ce_leg.ltp == 150.5
    assert ce_leg.delta is None  # always None — computed locally via greeks_engine


def test_get_option_chain_returns_empty_for_unknown_underlying(tmp_path):
    adapter = _adapter_with_seeded_master(tmp_path)
    adapter._smart = MagicMock()
    legs = adapter.get_option_chain("BANKNIFTY", "28OCT2025")
    assert legs == []


def test_place_order_resolves_symboltoken_from_instrument_master(tmp_path):
    adapter = _adapter_with_seeded_master(tmp_path)
    adapter.paper_mode = False  # exercise the real order-building path
    adapter._smart = MagicMock()
    adapter._smart.placeOrder.return_value = "ORDER123"

    result = adapter.place_entry_buy("NIFTY28OCT2524400CE", 75, 150.0, tag="test")

    assert result.ok is True
    assert result.order_id == "ORDER123"
    call_args = adapter._smart.placeOrder.call_args[0][0]
    assert call_args["symboltoken"] == "58784"
    assert call_args["exchange"] == "NFO"


def test_place_order_fails_cleanly_when_symbol_not_in_master(tmp_path):
    adapter = _adapter_with_seeded_master(tmp_path)
    adapter.paper_mode = False
    adapter._smart = MagicMock()

    result = adapter.place_entry_buy("UNKNOWN25AUG25000CE", 75, 150.0, tag="test")

    assert result.ok is False
    assert "symboltoken" in result.detail
    adapter._smart.placeOrder.assert_not_called()  # never even attempted with a bad token
