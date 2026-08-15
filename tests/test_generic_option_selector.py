from __future__ import annotations

from unittest.mock import MagicMock

from brokers.base import OptionLeg
from execution.generic_option_selector import GenericOptionSelector
from execution.greeks_engine import black_scholes_price


def test_uses_broker_supplied_delta_when_present():
    adapter = MagicMock()
    adapter.get_option_chain.return_value = [
        OptionLeg(symbol="X25000CE", strike_price=25000, option_type="CE", ltp=150.0, delta=0.55),
    ]
    selector = GenericOptionSelector(adapter, "NIFTY", strike_interval=50)
    result = selector.select(expiry="123", current_spot=25000, option_type="CE")

    assert result.delta == 0.55
    assert result.delta_is_estimated is False


def test_falls_back_to_computed_delta_when_leg_delta_is_none():
    from datetime import datetime, timedelta, timezone
    spot, strike = 25000, 25000
    expiry_epoch = (datetime.now(timezone.utc) + timedelta(days=7)).timestamp()
    t_years = 7 / 365.25
    ltp = black_scholes_price(spot, strike, t_years, 0.15, 0.065, "CE")

    adapter = MagicMock()
    adapter.get_option_chain.return_value = [
        OptionLeg(symbol="X25000CE", strike_price=strike, option_type="CE",
                   ltp=ltp, delta=None, expiry_epoch_seconds=expiry_epoch),
    ]
    selector = GenericOptionSelector(adapter, "NIFTY", strike_interval=50)
    result = selector.select(expiry=str(expiry_epoch), current_spot=spot, option_type="CE")

    assert result is not None
    assert result.delta_is_estimated is True
    assert 0.45 < result.delta < 0.55


def test_this_works_identically_regardless_of_which_adapter_is_passed():
    """The whole point: GenericOptionSelector doesn't know or care whether
    the adapter is Fyers, AngelOne, or anything else — it only calls
    get_option_chain(), which every BrokerAdapter implements."""
    fyers_like_adapter = MagicMock()
    fyers_like_adapter.get_option_chain.return_value = [
        OptionLeg(symbol="FYERS_SYM", strike_price=25000, option_type="CE", ltp=150.0, delta=0.55),
    ]
    angelone_like_adapter = MagicMock()
    angelone_like_adapter.get_option_chain.return_value = [
        OptionLeg(symbol="ANGELONE_SYM", strike_price=25000, option_type="CE", ltp=150.0, delta=0.55),
    ]

    for adapter, expected_symbol in [(fyers_like_adapter, "FYERS_SYM"), (angelone_like_adapter, "ANGELONE_SYM")]:
        selector = GenericOptionSelector(adapter, "NIFTY", strike_interval=50)
        result = selector.select(expiry="123", current_spot=25000, option_type="CE")
        assert result.symbol == expected_symbol


def test_cache_avoids_refetching_when_spot_barely_moves():
    adapter = MagicMock()
    adapter.get_option_chain.return_value = [
        OptionLeg(symbol="X25000CE", strike_price=25000, option_type="CE", ltp=150.0, delta=0.55),
    ]
    selector = GenericOptionSelector(adapter, "NIFTY", strike_interval=50, cache_refresh_seconds=999)

    selector.select(expiry="123", current_spot=25000, option_type="CE")
    selector.select(expiry="123", current_spot=25010, option_type="CE")  # tiny move, within strike_interval

    adapter.get_option_chain.assert_called_once()  # only the first call actually hit the adapter


def test_cache_refreshes_when_spot_crosses_strike_interval():
    adapter = MagicMock()
    adapter.get_option_chain.return_value = [
        OptionLeg(symbol="X25000CE", strike_price=25000, option_type="CE", ltp=150.0, delta=0.55),
    ]
    selector = GenericOptionSelector(adapter, "NIFTY", strike_interval=50, cache_refresh_seconds=999)

    selector.select(expiry="123", current_spot=25000, option_type="CE")
    selector.select(expiry="123", current_spot=25060, option_type="CE")  # crossed the 50pt interval

    assert adapter.get_option_chain.call_count == 2
