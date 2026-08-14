from __future__ import annotations

from datetime import datetime, timedelta, timezone

from execution.greeks_engine import (
    black_scholes_price,
    compute_delta_from_price,
    implied_volatility,
    years_to_expiry,
)


def _expiry_in(days: int) -> float:
    return (datetime.now(timezone.utc) + timedelta(days=days)).timestamp()


def test_atm_call_delta_is_approximately_half():
    spot = 25000
    t_years = 7 / 365.25
    price = black_scholes_price(spot, 25000, t_years, 0.15, 0.065, "CE")
    delta = compute_delta_from_price(price, spot, 25000, _expiry_in(7), "CE")
    assert 0.45 < delta < 0.55


def test_deep_itm_call_delta_approaches_one():
    spot = 25000
    t_years = 7 / 365.25
    price = black_scholes_price(spot, 22000, t_years, 0.15, 0.065, "CE")
    delta = compute_delta_from_price(price, spot, 22000, _expiry_in(7), "CE")
    assert delta > 0.95


def test_deep_otm_call_delta_approaches_zero():
    spot = 25000
    t_years = 7 / 365.25
    price = black_scholes_price(spot, 28000, t_years, 0.15, 0.065, "CE")
    delta = compute_delta_from_price(price, spot, 28000, _expiry_in(7), "CE")
    assert delta < 0.05


def test_atm_put_delta_is_approximately_half_absolute():
    spot = 25000
    t_years = 7 / 365.25
    price = black_scholes_price(spot, 25000, t_years, 0.15, 0.065, "PE")
    delta = compute_delta_from_price(price, spot, 25000, _expiry_in(7), "PE")
    assert 0.45 < delta < 0.55  # returned as abs()


def test_implied_volatility_round_trips_known_input():
    spot, strike, t_years = 25000, 25000, 7 / 365.25
    known_iv = 0.20
    price = black_scholes_price(spot, strike, t_years, known_iv, 0.065, "CE")
    result = implied_volatility(price, spot, strike, t_years, "CE")
    assert result.converged is True
    assert abs(result.iv - known_iv) < 0.001


def test_implied_volatility_handles_zero_price_without_crashing():
    result = implied_volatility(0.0, 25000, 25000, 0.02, "CE")
    assert result.converged is False
    assert result.iv > 0  # still returns a usable positive fallback


def test_years_to_expiry_floors_at_small_positive_on_expiry_day():
    past_or_now = datetime.now(timezone.utc).timestamp()
    t = years_to_expiry(past_or_now)
    assert t > 0  # never zero or negative — keeps Black-Scholes well-defined


def test_years_to_expiry_handles_missing_value():
    t = years_to_expiry(None)
    assert t > 0
