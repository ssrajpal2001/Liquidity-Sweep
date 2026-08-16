from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import backtest.run_diagnostic_backtest as rdb


def test_fetch_chunked_recovers_from_transient_rate_limit_errors():
    """Regression test for a real bug found live: firing ~25 chunk
    requests back-to-back with zero delay hit AngelOne's rate limiter
    almost immediately, silently dropping 12+ of 25 chunks in a real
    2-year run. Must recover from transient rate-limit errors via retry."""
    with patch.object(rdb, "RATE_LIMIT_DELAY_SECONDS", 0), \
         patch.object(rdb, "RATE_LIMIT_BACKOFF_SECONDS", 0):

        mock_adapter = MagicMock()
        call_log = []

        def fake_fetch(symbol, from_date, to_date):
            call_log.append((from_date, to_date))
            if len(call_log) <= 2:
                raise Exception("Access denied because of exceeding access rate")
            return [(1000.0, 1, 2, 0.5, 1.5, 100)]

        mock_adapter.get_historical_candles.side_effect = fake_fetch

        result = rdb._fetch_chunked(mock_adapter, "NIFTY", date(2024, 8, 15), date(2024, 8, 20))

        assert len(call_log) == 3  # 1 initial + 2 retries before success
        assert result == [(1000.0, 1, 2, 0.5, 1.5, 100)]


def test_fetch_chunked_gives_up_after_max_retries():
    with patch.object(rdb, "RATE_LIMIT_DELAY_SECONDS", 0), \
         patch.object(rdb, "RATE_LIMIT_BACKOFF_SECONDS", 0):

        mock_adapter = MagicMock()
        mock_adapter.get_historical_candles.side_effect = Exception(
            "Access denied because of exceeding access rate"
        )

        result = rdb._fetch_chunked(mock_adapter, "NIFTY", date(2024, 8, 15), date(2024, 8, 20))

        assert result == []
        # 1 initial attempt + RATE_LIMIT_MAX_RETRIES retries
        assert mock_adapter.get_historical_candles.call_count == 1 + rdb.RATE_LIMIT_MAX_RETRIES


def test_fetch_chunked_does_not_retry_non_rate_limit_errors():
    """A genuine, non-rate-limit error (bad symbol, auth failure, etc.)
    should fail fast on that chunk, not burn through retries meant only
    for transient rate-limit conditions."""
    with patch.object(rdb, "RATE_LIMIT_DELAY_SECONDS", 0), \
         patch.object(rdb, "RATE_LIMIT_BACKOFF_SECONDS", 0):

        mock_adapter = MagicMock()
        mock_adapter.get_historical_candles.side_effect = Exception("Invalid symbol token")

        result = rdb._fetch_chunked(mock_adapter, "NIFTY", date(2024, 8, 15), date(2024, 8, 20))

        assert result == []
        assert mock_adapter.get_historical_candles.call_count == 1  # no retries for a non-rate-limit error


def test_fetch_chunked_paces_every_request_not_just_failed_ones():
    """The per-chunk delay must apply to every successful request too —
    not just the ones that get rate-limited — otherwise a long run still
    fires requests back-to-back and re-triggers the limiter."""
    with patch.object(rdb, "RATE_LIMIT_DELAY_SECONDS", 0.01), \
         patch.object(rdb, "RATE_LIMIT_BACKOFF_SECONDS", 0), \
         patch("time.sleep") as mock_sleep:

        mock_adapter = MagicMock()
        mock_adapter.get_historical_candles.return_value = [(1000.0, 1, 2, 0.5, 1.5, 100)]

        rdb._fetch_chunked(mock_adapter, "NIFTY", date(2024, 8, 15), date(2024, 9, 20))  # spans 2 chunks

        # At least one pacing sleep call per chunk, even though nothing failed.
        pacing_calls = [c for c in mock_sleep.call_args_list if c.args[0] == 0.01]
        assert len(pacing_calls) >= 2
