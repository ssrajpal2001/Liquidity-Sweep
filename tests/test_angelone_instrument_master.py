from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from execution.angelone_instrument_master import AngelOneInstrumentMaster

# Verbatim samples from real, independently posted forum data.
SAMPLE_ROWS = [
    {"token": "58784", "symbol": "NIFTY28OCT2524400CE", "name": "NIFTY", "expiry": "28OCT2025",
     "strike": "2440000.000000", "lotsize": "75", "instrumenttype": "OPTIDX", "exch_seg": "NFO",
     "tick_size": "5.000000"},
    {"token": "58785", "symbol": "NIFTY28OCT2524400PE", "name": "NIFTY", "expiry": "28OCT2025",
     "strike": "2440000.000000", "lotsize": "75", "instrumenttype": "OPTIDX", "exch_seg": "NFO",
     "tick_size": "5.000000"},
    {"token": "58800", "symbol": "NIFTY28OCT2524500CE", "name": "NIFTY", "expiry": "28OCT2025",
     "strike": "2450000.000000", "lotsize": "75", "instrumenttype": "OPTIDX", "exch_seg": "NFO",
     "tick_size": "5.000000"},
    {"token": "60000", "symbol": "NIFTY04NOV2524400CE", "name": "NIFTY", "expiry": "04NOV2025",
     "strike": "2440000.000000", "lotsize": "75", "instrumenttype": "OPTIDX", "exch_seg": "NFO",
     "tick_size": "5.000000"},
    {"token": "3045", "symbol": "SBIN-EQ", "name": "SBIN", "expiry": "",
     "strike": "-1.000000", "lotsize": "1", "instrumenttype": "", "exch_seg": "NSE",
     "tick_size": "5.000000"},
]


@pytest.fixture()
def master(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(SAMPLE_ROWS))
    m = AngelOneInstrumentMaster(cache_path=cache_path)
    return m


def test_strike_is_correctly_divided_by_100(master):
    strikes = master.strikes_for_expiry("NIFTY", date(2025, 10, 28))
    values = sorted({s.strike for s in strikes})
    assert values == [24400.0, 24500.0]  # NOT 2440000.0 — the x100 quirk handled correctly


def test_expiry_string_parsed_correctly(master):
    expiries = master.option_expiries("NIFTY")
    assert date(2025, 10, 28) in expiries
    assert date(2025, 11, 4) in expiries
    assert len(expiries) == 2


def test_nearest_option_expiry_picks_soonest_future_date(master):
    nearest = master.nearest_option_expiry("NIFTY", today=date(2025, 10, 1))
    assert nearest == date(2025, 10, 28)


def test_nearest_option_expiry_skips_past_dates(master):
    nearest = master.nearest_option_expiry("NIFTY", today=date(2025, 10, 29))
    assert nearest == date(2025, 11, 4)  # the 28OCT one is now in the past


def test_nearest_option_expiry_returns_none_when_no_future_expiries(master):
    nearest = master.nearest_option_expiry("NIFTY", today=date(2026, 1, 1))
    assert nearest is None


def test_strikes_for_expiry_excludes_equity_and_wrong_name(master):
    strikes = master.strikes_for_expiry("NIFTY", date(2025, 10, 28))
    symbols = [s.symbol for s in strikes]
    assert "SBIN-EQ" not in symbols
    assert all("NIFTY" in s for s in symbols)


def test_find_by_symbol_returns_correct_entry(master):
    entry = master.find_by_symbol("SBIN-EQ")
    assert entry is not None
    assert entry.token == "3045"
    assert entry.exch_seg == "NSE"


def test_find_by_symbol_returns_none_for_unknown_symbol(master):
    assert master.find_by_symbol("DOES-NOT-EXIST") is None


def test_cache_is_reused_within_max_age(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(SAMPLE_ROWS))
    master = AngelOneInstrumentMaster(cache_path=cache_path)

    with patch("execution.angelone_instrument_master.requests.get") as mock_get:
        master.option_expiries("NIFTY")  # first call: loads from cache file
        mock_get.assert_not_called()  # must NOT hit the network — cache is fresh


def test_downloads_fresh_when_cache_missing(tmp_path):
    cache_path = tmp_path / "cache.json"  # does not exist yet
    master = AngelOneInstrumentMaster(cache_path=cache_path)

    mock_response = MagicMock()
    mock_response.json.return_value = SAMPLE_ROWS
    mock_response.raise_for_status.return_value = None

    with patch("execution.angelone_instrument_master.requests.get", return_value=mock_response) as mock_get:
        expiries = master.option_expiries("NIFTY")
        mock_get.assert_called_once()
    assert len(expiries) == 2
    assert cache_path.exists()  # downloaded data got cached to disk


def test_spot_index_token_lookup():
    assert AngelOneInstrumentMaster.spot_index_token("NIFTY", "NSE") == "99926000"
    assert AngelOneInstrumentMaster.spot_index_token("UNKNOWN", "NSE") is None


def test_malformed_rows_are_skipped_not_crashed_on(tmp_path):
    bad_rows = SAMPLE_ROWS + [{"symbol": "MISSING_TOKEN_FIELD"}]  # missing required 'token' key
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(bad_rows))
    master = AngelOneInstrumentMaster(cache_path=cache_path)
    entries = master._load()  # should not raise
    assert len(entries) == len(SAMPLE_ROWS)  # bad row silently dropped, good ones kept
