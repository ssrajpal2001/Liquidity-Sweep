from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pyotp
import pytest

from brokers.angelone_adapter import AngelOneBrokerAdapter
from brokers.fyers_adapter import FyersBrokerAdapter
from config.config_loader import load_settings
from main import TradingSession, _assert_paper_gate


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        """
app:
  name: "test-bot"
  environment: "paper"
  timezone: "Asia/Kolkata"
capital:
  total_capital_inr: 200000
  risk_per_trade_pct: 1.0
  max_daily_loss_pct: 3.0
  max_trades_per_day: 3
instruments:
  - name: "NIFTY"
    spot_key: "NSE:NIFTY50-INDEX"
    lot_size: 75
    strike_interval: 50
timeframes:
  htf_minutes: 75
  itf_minutes: 15
  ltf_minutes: [3, 5]
session:
  market_open: "09:15"
  market_close: "15:30"
  high_probability_windows: [["09:15", "10:30"]]
  blocked_window: [["11:30", "13:00"]]
option_selection:
  target_delta_min: 0.5
  target_delta_max: 0.6
  delta_cache_refresh_seconds: 120
execution:
  order_type: "LIMIT"
  limit_offset_rupees: 1.5
  target1_rr: 1.5
  target1_booking_pct: 60
risk:
  sweep_buffer_points: {NIFTY: 4}
  atr_displacement_multiplier: 1.5
""",
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    env_path.write_text(
        "FYERS_CLIENT_ID=XC1234-100\nFYERS_SECRET_KEY=s\nFYERS_REDIRECT_URI=https://x\nPAPER_MODE=true\n",
        encoding="utf-8",
    )
    for var in ("FYERS_CLIENT_ID", "FYERS_SECRET_KEY", "FYERS_REDIRECT_URI"):
        monkeypatch.delenv(var, raising=False)
    return load_settings(settings_path=settings_path, env_path=env_path)


def _fyers_adapter(settings):
    adapter = FyersBrokerAdapter(settings.env, paper_mode=True)
    adapter.rest_client._model = MagicMock()
    return adapter


def _angelone_adapter():
    env = SimpleNamespace(
        api_key="k", client_code="A1", pin="1234", totp_secret=pyotp.random_base32(),
        token_store_path=Path("/tmp/test_angelone_token.json"),
    )
    return AngelOneBrokerAdapter(env, paper_mode=True)


def test_trading_session_builds_with_fyers_adapter(settings):
    session = TradingSession(settings, _fyers_adapter(settings))
    assert session.broker.broker_name == "fyers"
    assert "NSE:NIFTY50-INDEX" in session.state_machines


def test_trading_session_builds_with_angelone_adapter(settings):
    session = TradingSession(settings, _angelone_adapter())
    assert session.broker.broker_name == "angelone"
    assert "NSE:NIFTY50-INDEX" in session.state_machines


def test_same_session_class_works_identically_regardless_of_broker(settings):
    """The actual point of the refactor: TradingSession never imports or
    references a broker-specific class — swapping the adapter is the
    entire integration."""
    for adapter in (_fyers_adapter(settings), _angelone_adapter()):
        session = TradingSession(settings, adapter)
        assert isinstance(session.option_selectors["NSE:NIFTY50-INDEX"], object)
        assert session.daily_guard.can_trade() == (True, "OK")
        assert session.position_manager.order_manager is adapter  # BrokerAdapter itself IS the order manager


def test_paper_gate_blocks_live_environment(settings):
    settings.raw["app"]["environment"] = "live"
    with pytest.raises(RuntimeError, match="paper"):
        _assert_paper_gate(settings)


def test_ws_reconnect_resync_calls_broker_for_every_instrument(settings):
    adapter = _fyers_adapter(settings)
    session = TradingSession(settings, adapter)

    now = datetime.now(timezone.utc)
    raw = [
        ((now - timedelta(minutes=30 - i)).timestamp(), 25000 + i, 25002 + i, 24998 + i, 25001 + i, 1000)
        for i in range(30)
    ]
    adapter.get_historical_candles = MagicMock(return_value=raw)

    session._rest_resync_on_reconnect()

    adapter.get_historical_candles.assert_called_once()
    call_args = adapter.get_historical_candles.call_args[0]
    assert call_args[0] == "NSE:NIFTY50-INDEX"


def test_ws_reconnect_resync_handles_empty_response_gracefully(settings):
    adapter = _fyers_adapter(settings)
    session = TradingSession(settings, adapter)
    adapter.get_historical_candles = MagicMock(return_value=[])

    session._rest_resync_on_reconnect()  # must not raise


def test_ws_reconnect_resync_handles_broker_exception_gracefully(settings):
    adapter = _fyers_adapter(settings)
    session = TradingSession(settings, adapter)
    adapter.get_historical_candles = MagicMock(side_effect=RuntimeError("network down"))

    session._rest_resync_on_reconnect()  # must not raise, must not crash the session


def test_ws_reconnect_resync_only_uses_recent_lookback_window(settings):
    """Old candles (outside RESYNC_LOOKBACK_MINUTES) should be filtered
    out — no need to replay the whole session's history on every
    reconnect."""
    import main as main_module

    adapter = _fyers_adapter(settings)
    session = TradingSession(settings, adapter)

    now = datetime.now(timezone.utc)
    old_candle = ((now - timedelta(hours=10)).timestamp(), 100, 102, 98, 101, 1000)
    recent_candle = ((now - timedelta(minutes=5)).timestamp(), 200, 202, 198, 201, 1000)
    adapter.get_historical_candles = MagicMock(return_value=[old_candle, recent_candle])

    bootstrap_calls = []
    session.candle_agg.bootstrap_instrument = lambda key, candles: bootstrap_calls.append(candles)

    session._rest_resync_on_reconnect()

    assert len(bootstrap_calls) == 1
    passed_candles = bootstrap_calls[0]
    assert len(passed_candles) == 1  # only the recent one survived the lookback filter
    assert passed_candles[0][1] == 200
