from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

from strategy.filters import is_within_trading_window

IST = timezone(timedelta(hours=5, minutes=30))

SESSION_CONFIG = {
    "high_probability_windows": [["09:15", "10:30"], ["13:15", "14:45"]],
    "blocked_window": [["11:30", "13:00"]],
}


def test_historical_morning_timestamp_passes_regardless_of_when_checked():
    """This is the exact scenario from a real backtest run: a signal's
    OWN historical timestamp (09:24 IST, inside the high-probability
    window) must pass the filter, even though the code evaluating it may
    actually run at a completely different wall-clock time (e.g. 20:48
    IST that evening, when the backtest script was executed). Regression
    test for a real bug: strategy/state_machine.py used to call
    datetime.now() here instead of the candle's own open_time, which
    silently discarded every backtested signal based on when the script
    happened to run rather than when the signal actually occurred."""
    historical_signal_time = datetime(2026, 8, 10, 9, 24, tzinfo=IST)
    allowed, reason = is_within_trading_window(historical_signal_time, SESSION_CONFIG)
    assert allowed is True
    assert "high-probability window" in reason


def test_historical_evening_timestamp_correctly_fails():
    """Confirms the filter is still doing real work — an actually-outside-
    hours historical timestamp should still be rejected."""
    historical_signal_time = datetime(2026, 8, 10, 20, 48, tzinfo=IST)
    allowed, reason = is_within_trading_window(historical_signal_time, SESSION_CONFIG)
    assert allowed is False
