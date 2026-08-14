from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from execution.greeks_engine import black_scholes_price
from execution.option_selector import OptionSelector


def _expiry_in(days: int) -> float:
    return (datetime.now(timezone.utc) + timedelta(days=days)).timestamp()


def _fake_chain_response(rows: list[dict]) -> dict:
    return {"s": "ok", "data": {"optionsChain": rows}}


def test_uses_broker_supplied_delta_when_present():
    """If the broker DOES give delta (contradicting the worry that no
    broker ever will), it's used directly — no unnecessary computation."""
    model = MagicMock()
    model.optionchain.return_value = _fake_chain_response([
        {"symbol": "NSE:NIFTY25AUG25000CE", "option_type": "CE", "strike_price": 25000,
         "ltp": 150.0, "delta": 0.55},
    ])
    selector = OptionSelector(model, "NSE:NIFTY50-INDEX", strike_interval=50)
    result = selector.select(expiry="1234567890", current_spot=25000, option_type="CE")

    assert result is not None
    assert result.delta == 0.55
    assert result.delta_is_estimated is False


def test_falls_back_to_computed_delta_when_broker_gives_none():
    """THE core fix: no delta field anywhere in the response -> Black-
    Scholes computes it from the option's own live LTP instead of
    guessing a flat constant. This is what makes the strategy not depend
    on whether Fyers (or any broker) supplies Greeks."""
    spot = 25000
    strike = 25000
    expiry = _expiry_in(7)
    t_years = 7 / 365.25
    synthetic_ltp = black_scholes_price(spot, strike, t_years, 0.15, 0.065, "CE")

    model = MagicMock()
    model.optionchain.return_value = _fake_chain_response([
        {"symbol": "NSE:NIFTY25AUG25000CE", "option_type": "CE", "strike_price": strike,
         "ltp": synthetic_ltp, "delta": None},  # broker gives nothing
    ])
    selector = OptionSelector(model, "NSE:NIFTY50-INDEX", strike_interval=50)
    result = selector.select(expiry=str(expiry), current_spot=spot, option_type="CE")

    assert result is not None
    assert result.delta_is_estimated is True
    assert 0.45 < result.delta < 0.55  # ATM strike -> computed delta should still land near 0.5


def test_picks_best_strike_within_target_range_across_mixed_delta_sources():
    """Some strikes have broker delta, some don't -> selection still
    works uniformly across both."""
    spot = 25000
    expiry = _expiry_in(7)
    t_years = 7 / 365.25

    itm_price = black_scholes_price(spot, 25000, t_years, 0.15, 0.065, "CE")  # ATM, broker gives no delta here

    model = MagicMock()
    model.optionchain.return_value = _fake_chain_response([
        {"symbol": "NSE:NIFTY25AUG24600CE", "option_type": "CE", "strike_price": 24600,
         "ltp": 500.0, "delta": 0.85},   # too deep ITM, outside target range
        {"symbol": "NSE:NIFTY25AUG25000CE", "option_type": "CE", "strike_price": 25000,
         "ltp": itm_price, "delta": None},  # no broker delta -> computed, should land in range
        {"symbol": "NSE:NIFTY25AUG25400CE", "option_type": "CE", "strike_price": 25400,
         "ltp": 50.0, "delta": 0.20},    # too far OTM, outside target range
    ])
    selector = OptionSelector(model, "NSE:NIFTY50-INDEX", strike_interval=50,
                               target_delta_min=0.50, target_delta_max=0.60)
    result = selector.select(expiry=str(expiry), current_spot=spot, option_type="CE")

    assert result is not None
    assert result.symbol == "NSE:NIFTY25AUG25000CE"
    assert result.delta_is_estimated is True


def test_returns_none_when_no_strike_lands_in_target_range():
    model = MagicMock()
    model.optionchain.return_value = _fake_chain_response([
        {"symbol": "X", "option_type": "CE", "strike_price": 25000, "ltp": 10.0, "delta": 0.05},
    ])
    selector = OptionSelector(model, "NSE:NIFTY50-INDEX", strike_interval=50,
                               target_delta_min=0.50, target_delta_max=0.60)
    result = selector.select(expiry="123", current_spot=25000, option_type="CE")
    assert result is None
