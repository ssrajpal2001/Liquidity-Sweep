"""
tests/test_config_and_auth.py

Sanity tests for Phase 0/1: config loading/validation and the token
expiry/storage logic. These do NOT hit the real Upstox API (no network) —
Phase 2+ will add integration tests behind a sandbox-only marker.
"""
from __future__ import annotations

import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from auth.auth import IST, TokenRecord, TokenStore, compute_expiry
from config.config_loader import ConfigError, load_settings

MINIMAL_YAML = textwrap.dedent(
    """
    app:
      name: "test-bot"
      environment: "sandbox"
      timezone: "Asia/Kolkata"
    capital:
      total_capital_inr: 100000
      risk_per_trade_pct: 1.0
      max_daily_loss_pct: 3.0
      max_trades_per_day: 3
    instruments:
      - name: "NIFTY"
        spot_key: "NSE_INDEX|Nifty 50"
        option_segment: "NSE_FO"
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
        "UPSTOX_API_KEY=test_key\n"
        "UPSTOX_API_SECRET=test_secret\n"
        "UPSTOX_REDIRECT_URI=https://example.com/callback\n"
        "UPSTOX_SANDBOX=true\n"
        f"TOKEN_STORE_PATH={tmp_path / 'token_store.json'}\n",
        encoding="utf-8",
    )
    return settings_path, env_path


def test_load_settings_success(project_files, monkeypatch):
    settings_path, env_path = project_files
    # Ensure a stray real env var from the host machine doesn't mask the
    # missing-var test elsewhere in this module.
    for var in ("UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_REDIRECT_URI"):
        monkeypatch.delenv(var, raising=False)

    settings = load_settings(settings_path=settings_path, env_path=env_path)
    assert settings.environment == "sandbox"
    assert settings.env.sandbox is True
    assert settings.instrument("NIFTY")["lot_size"] == 75


def test_load_settings_missing_env_var_raises(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(MINIMAL_YAML, encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("UPSTOX_API_KEY=only_this_one\n", encoding="utf-8")

    for var in ("UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_REDIRECT_URI"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(ConfigError, match="Missing required environment variable"):
        load_settings(settings_path=settings_path, env_path=env_path)


def test_load_settings_missing_yaml_section_raises(tmp_path, monkeypatch):
    bad_yaml = tmp_path / "settings.yaml"
    bad_yaml.write_text("app:\n  environment: sandbox\n  timezone: Asia/Kolkata\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text(
        "UPSTOX_API_KEY=k\nUPSTOX_API_SECRET=s\nUPSTOX_REDIRECT_URI=https://x\n",
        encoding="utf-8",
    )
    for var in ("UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_REDIRECT_URI"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(ConfigError, match="missing section"):
        load_settings(settings_path=bad_yaml, env_path=env_path)


# -- auth: token expiry math -------------------------------------------------

def test_compute_expiry_evening_generation_expires_next_morning():
    generated = datetime(2026, 8, 18, 20, 0, tzinfo=IST)  # 8 PM Tuesday
    expiry = compute_expiry(generated)
    assert expiry == datetime(2026, 8, 19, 3, 30, tzinfo=IST)  # 3:30 AM Wednesday


def test_compute_expiry_early_morning_generation_expires_same_day():
    generated = datetime(2026, 8, 19, 2, 30, tzinfo=IST)  # 2:30 AM Wednesday
    expiry = compute_expiry(generated)
    assert expiry == datetime(2026, 8, 19, 3, 30, tzinfo=IST)  # 3:30 AM same Wednesday


def test_compute_expiry_handles_non_ist_input():
    generated_utc = datetime(2026, 8, 18, 14, 30, tzinfo=timezone.utc)  # 20:00 IST
    expiry = compute_expiry(generated_utc)
    assert expiry == datetime(2026, 8, 19, 3, 30, tzinfo=IST)


# -- auth: token store round-trip -------------------------------------------

def test_token_store_round_trip_and_expiry(tmp_path):
    store = TokenStore(tmp_path / "token_store.json")
    assert store.load() is None

    future = (datetime.now(IST) + timedelta(hours=2)).isoformat()
    record = TokenRecord(
        access_token="abc123",
        generated_at=datetime.now(IST).isoformat(),
        expires_at=future,
    )
    store.save(record)

    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "abc123"
    assert loaded.is_expired() is False

    store.clear()
    assert store.load() is None


def test_token_record_is_expired_true_for_past_expiry():
    past = (datetime.now(IST) - timedelta(hours=1)).isoformat()
    record = TokenRecord(
        access_token="abc123",
        generated_at=(datetime.now(IST) - timedelta(hours=6)).isoformat(),
        expires_at=past,
    )
    assert record.is_expired() is True
