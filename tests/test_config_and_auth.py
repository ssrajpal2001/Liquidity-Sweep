from __future__ import annotations

import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from auth.auth import IST, SOFT_EXPIRY_HOURS, TokenRecord, TokenStore, compute_soft_expiry
from config.config_loader import ConfigError, load_settings

MINIMAL_YAML = textwrap.dedent(
    """
    app:
      name: "test-bot"
      environment: "paper"
      timezone: "Asia/Kolkata"
    capital:
      total_capital_inr: 100000
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
    """
)


@pytest.fixture()
def project_files(tmp_path: Path):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(MINIMAL_YAML, encoding="utf-8")

    env_path = tmp_path / ".env"
    env_path.write_text(
        "FYERS_CLIENT_ID=test_client_id\n"
        "FYERS_SECRET_KEY=test_secret_key\n"
        "FYERS_REDIRECT_URI=https://example.com/callback\n"
        "PAPER_MODE=true\n"
        f"TOKEN_STORE_PATH={tmp_path / 'token_store.json'}\n",
        encoding="utf-8",
    )
    return settings_path, env_path


def _clear_fyers_env(monkeypatch):
    for var in ("FYERS_CLIENT_ID", "FYERS_SECRET_KEY", "FYERS_REDIRECT_URI"):
        monkeypatch.delenv(var, raising=False)


def test_load_settings_success(project_files, monkeypatch):
    settings_path, env_path = project_files
    _clear_fyers_env(monkeypatch)

    settings = load_settings(settings_path=settings_path, env_path=env_path)
    assert settings.environment == "paper"
    assert settings.env.paper_mode is True
    assert settings.instrument("NIFTY")["lot_size"] == 75
    assert settings.instrument("NIFTY")["spot_key"] == "NSE:NIFTY50-INDEX"


def test_load_settings_missing_env_var_raises(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(MINIMAL_YAML, encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("FYERS_CLIENT_ID=only_this_one\n", encoding="utf-8")

    _clear_fyers_env(monkeypatch)

    with pytest.raises(ConfigError, match="Missing required environment variable"):
        load_settings(settings_path=settings_path, env_path=env_path)


def test_load_settings_missing_yaml_section_raises(tmp_path, monkeypatch):
    bad_yaml = tmp_path / "settings.yaml"
    bad_yaml.write_text("app:\n  environment: paper\n  timezone: Asia/Kolkata\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text(
        "FYERS_CLIENT_ID=k\nFYERS_SECRET_KEY=s\nFYERS_REDIRECT_URI=https://x\n",
        encoding="utf-8",
    )
    _clear_fyers_env(monkeypatch)

    with pytest.raises(ConfigError, match="missing section"):
        load_settings(settings_path=bad_yaml, env_path=env_path)


def test_load_settings_rejects_bad_environment_value(tmp_path, monkeypatch):
    bad_yaml_text = MINIMAL_YAML.replace('environment: "paper"', 'environment: "sandbox"')
    bad_yaml = tmp_path / "settings.yaml"
    bad_yaml.write_text(bad_yaml_text, encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text(
        "FYERS_CLIENT_ID=k\nFYERS_SECRET_KEY=s\nFYERS_REDIRECT_URI=https://x\n",
        encoding="utf-8",
    )
    _clear_fyers_env(monkeypatch)

    with pytest.raises(ConfigError, match="must be 'paper' or 'live'"):
        load_settings(settings_path=bad_yaml, env_path=env_path)


# -- auth: soft-expiry backstop math -----------------------------------------

def test_compute_soft_expiry_is_generated_at_plus_backstop_hours():
    generated = datetime(2026, 8, 18, 8, 0, tzinfo=IST)
    expiry = compute_soft_expiry(generated)
    assert expiry == generated + timedelta(hours=SOFT_EXPIRY_HOURS)


def test_compute_soft_expiry_handles_non_ist_input():
    generated_utc = datetime(2026, 8, 18, 3, 0, tzinfo=timezone.utc)  # 08:30 IST
    expiry = compute_soft_expiry(generated_utc)
    expected = generated_utc.astimezone(IST) + timedelta(hours=SOFT_EXPIRY_HOURS)
    assert expiry == expected


# -- auth: token store round-trip and combined_token() ------------------------

def test_token_store_round_trip_and_expiry(tmp_path):
    store = TokenStore(tmp_path / "token_store.json")
    assert store.load() is None

    future = (datetime.now(IST) + timedelta(hours=2)).isoformat()
    record = TokenRecord(
        access_token="abc123",
        generated_at=datetime.now(IST).isoformat(),
        soft_expires_at=future,
        client_id="XC1234-100",
    )
    store.save(record)

    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "abc123"
    assert loaded.is_soft_expired() is False
    assert loaded.combined_token() == "XC1234-100:abc123"

    store.clear()
    assert store.load() is None


def test_token_record_is_soft_expired_true_for_past_expiry():
    past = (datetime.now(IST) - timedelta(hours=1)).isoformat()
    record = TokenRecord(
        access_token="abc123",
        generated_at=(datetime.now(IST) - timedelta(hours=25)).isoformat(),
        soft_expires_at=past,
        client_id="XC1234-100",
    )
    assert record.is_soft_expired() is True


def test_combined_token_format_matches_fyers_sdk_expectation():
    """Fyers' FyersModel/FyersDataSocket both expect 'client_id:access_token'
    — verified against fyers-apiv3 sample code, not guessed."""
    record = TokenRecord(
        access_token="eyJhbGciOi...",
        generated_at=datetime.now(IST).isoformat(),
        soft_expires_at=(datetime.now(IST) + timedelta(hours=1)).isoformat(),
        client_id="XC1234-100",
    )
    assert record.combined_token() == "XC1234-100:eyJhbGciOi..."
