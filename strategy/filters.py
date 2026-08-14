"""
strategy/filters.py

Two independent, pure filters. Both read their thresholds from
config/settings.yaml (session.high_probability_windows / blocked_window)
so changing the time windows never requires a code change.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from enum import Enum

IST = timezone(timedelta(hours=5, minutes=30))


def _parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def is_within_trading_window(now: datetime, session_config: dict) -> tuple[bool, str]:
    """Returns (allowed, reason). `now` should be IST; if it isn't
    timezone-aware, it's assumed to already be IST wall-clock time."""
    now_time = now.timetz() if now.tzinfo else time(now.hour, now.minute)
    now_time = time(now_time.hour, now_time.minute)  # drop tzinfo/seconds for comparison

    for start_s, end_s in session_config.get("blocked_window", []):
        start, end = _parse_hhmm(start_s), _parse_hhmm(end_s)
        if start <= now_time < end:
            return False, f"Inside blocked window {start_s}-{end_s} (low-volume chop)"

    for start_s, end_s in session_config.get("high_probability_windows", []):
        start, end = _parse_hhmm(start_s), _parse_hhmm(end_s)
        if start <= now_time < end:
            return True, f"Inside high-probability window {start_s}-{end_s}"

    return False, "Outside all configured high-probability windows"


class Bias(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


def htf_bias_from_base(base_high: float, base_low: float, current_price: float) -> Bias:
    """Simple, explicit HTF bias proxy: where is price relative to the
    midpoint of the current rolling base. This is intentionally simple for
    Phase 5 — swap in a richer HTF-trend model later without touching the
    filter's call signature."""
    midpoint = (base_high + base_low) / 2
    if current_price > midpoint:
        return Bias.BULLISH
    if current_price < midpoint:
        return Bias.BEARISH
    return Bias.NEUTRAL


def is_bias_aligned(sweep_direction: str, bias: Bias) -> tuple[bool, str]:
    """Only take bearish sweeps (PUT setups) when HTF bias is bearish/
    neutral-leaning-down is NOT required — per the strategy notes, we only
    need to avoid trading a sweep directly against a strong opposing HTF
    trend. bearish sweep => want bias != BULLISH; bullish sweep => bias != BEARISH."""
    if sweep_direction == "bearish" and bias == Bias.BULLISH:
        return False, "Bearish sweep against a bullish HTF bias — skipped (counter-trend)"
    if sweep_direction == "bullish" and bias == Bias.BEARISH:
        return False, "Bullish sweep against a bearish HTF bias — skipped (counter-trend)"
    return True, f"Sweep direction {sweep_direction} aligned with HTF bias {bias.value}"
