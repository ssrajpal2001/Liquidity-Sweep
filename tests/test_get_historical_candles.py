from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from brokers.fyers_adapter import FyersBrokerAdapter
from brokers.angelone_adapter import AngelOneBrokerAdapter


def _fyers_env(tmp_path):
    from types import SimpleNamespace
    return SimpleNamespace(
        client_id="XC1234-100", secret_key="fake", redirect_uri="https://example.com",
        paper_mode=True, auth_code=None, token_store_path=tmp_path / "fyers_token.json",
    )


def _angelone_env(tmp_path):
    from types import SimpleNamespace
    import pyotp
    return SimpleNamespace(
        api_key="k", client_code="A1", pin="1234", totp_secret=pyotp.random_base32(),
        token_store_path=tmp_path / "angelone_session.json",
    )


def test_fyers_get_historical_candles_parses_response(tmp_path):
    adapter = FyersBrokerAdapter(_fyers_env(tmp_path), paper_mode=True)
    mock_model = MagicMock()
    mock_model.history.return_value = {
        "s": "ok",
        "candles": [[1786000000, 25000, 25010, 24990, 25005, 1000],
                    [1786000060, 25005, 25015, 24995, 25010, 1200]],
    }
    adapter.rest_client._model = mock_model

    result = adapter.get_historical_candles("NSE:NIFTY50-INDEX", date(2026, 8, 10), date(2026, 8, 11))

    assert len(result) == 2
    assert result[0] == (1786000000, 25000, 25010, 24990, 25005, 1000)


def test_fyers_get_historical_candles_returns_empty_on_api_error(tmp_path):
    adapter = FyersBrokerAdapter(_fyers_env(tmp_path), paper_mode=True)
    mock_model = MagicMock()
    mock_model.history.return_value = {"s": "error", "message": "bad symbol"}
    adapter.rest_client._model = mock_model

    result = adapter.get_historical_candles("NSE:BADSYMBOL", date(2026, 8, 10), date(2026, 8, 11))
    assert result == []


def test_fyers_get_historical_candles_handles_exception_gracefully(tmp_path):
    adapter = FyersBrokerAdapter(_fyers_env(tmp_path), paper_mode=True)
    mock_model = MagicMock()
    mock_model.history.side_effect = RuntimeError("network error")
    adapter.rest_client._model = mock_model

    result = adapter.get_historical_candles("NSE:NIFTY50-INDEX", date(2026, 8, 10), date(2026, 8, 11))
    assert result == []  # never raises, degrades to empty list


def test_angelone_get_historical_candles_parses_response(tmp_path):
    adapter = AngelOneBrokerAdapter(_angelone_env(tmp_path), paper_mode=True)
    adapter._smart = MagicMock()
    adapter._smart.getCandleData.return_value = {
        "status": True,
        "data": [
            ["2026-08-10T09:15:00+05:30", 25000, 25010, 24990, 25005, 1000],
            ["2026-08-10T09:16:00+05:30", 25005, 25015, 24995, 25010, 1200],
        ],
    }

    result = adapter.get_historical_candles("NIFTY", date(2026, 8, 10), date(2026, 8, 11))

    assert len(result) == 2
    assert result[0][1:] == (25000, 25010, 24990, 25005, 1000)  # OHLCV correct; [0] is epoch


def test_angelone_get_historical_candles_unknown_instrument_returns_empty(tmp_path):
    adapter = AngelOneBrokerAdapter(_angelone_env(tmp_path), paper_mode=True)
    adapter._smart = MagicMock()

    result = adapter.get_historical_candles("SOME_UNKNOWN_INDEX", date(2026, 8, 10), date(2026, 8, 11))
    assert result == []
    adapter._smart.getCandleData.assert_not_called()  # never even attempted without a resolvable token


def test_angelone_get_historical_candles_skips_malformed_rows(tmp_path):
    adapter = AngelOneBrokerAdapter(_angelone_env(tmp_path), paper_mode=True)
    adapter._smart = MagicMock()
    adapter._smart.getCandleData.return_value = {
        "status": True,
        "data": [
            ["2026-08-10T09:15:00+05:30", 25000, 25010, 24990, 25005, 1000],
            ["not-a-valid-timestamp", 1, 2, 3, 4, 5],
        ],
    }
    result = adapter.get_historical_candles("NIFTY", date(2026, 8, 10), date(2026, 8, 11))
    assert len(result) == 1  # bad row dropped, good row kept
