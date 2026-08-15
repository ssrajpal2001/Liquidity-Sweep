"""
execution/generic_option_selector.py

The broker-agnostic replacement for execution/option_selector.py's role
in main.py. That original module was written directly against a raw
Fyers model — this one works against ANY BrokerAdapter via
get_option_chain(), which is what actually makes "main.py runs the same
on Fyers or AngelOne" true. execution/option_selector.py itself is still
used internally by FyersBrokerAdapter.get_option_chain() to build the
normalized OptionLeg list — this module is one layer up, doing delta-
target strike selection + caching on top of whichever adapter is passed
in.

Delta selection: every OptionLeg from a broker MAY have delta=None (both
current adapters always return None, deliberately — see execution/
greeks_engine.py's docstring for why depending on broker Greeks was the
original risk this avoids). This module computes Delta locally via
Black-Scholes for any leg missing it, exactly the same fallback
option_selector.py already used for Fyers, now applied uniformly
regardless of broker.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from brokers.base import BrokerAdapter, OptionLeg
from execution.greeks_engine import compute_delta_from_price

logger = logging.getLogger(__name__)


@dataclass
class SelectedOption:
    symbol: str
    strike_price: float
    option_type: str
    ltp: float
    delta: float
    delta_is_estimated: bool
    fetched_at: float


class GenericOptionSelector:
    def __init__(
        self,
        broker_adapter: BrokerAdapter,
        underlying_symbol: str,
        strike_interval: float,
        target_delta_min: float = 0.50,
        target_delta_max: float = 0.60,
        cache_refresh_seconds: int = 120,
    ):
        self.adapter = broker_adapter
        self.underlying_symbol = underlying_symbol
        self.strike_interval = strike_interval
        self.target_delta_min = target_delta_min
        self.target_delta_max = target_delta_max
        self.cache_refresh_seconds = cache_refresh_seconds

        self._cache: dict[str, SelectedOption] = {}
        self._cache_spot_price: Optional[float] = None
        self._cache_fetched_at: float = 0.0

    def _cache_is_stale(self, current_spot: float) -> bool:
        if not self._cache:
            return True
        if time.time() - self._cache_fetched_at > self.cache_refresh_seconds:
            return True
        if self._cache_spot_price is None:
            return True
        return abs(current_spot - self._cache_spot_price) >= self.strike_interval

    def _pick_by_delta(self, chain: list[OptionLeg], option_type: str, spot: float) -> Optional[SelectedOption]:
        candidates = [leg for leg in chain if leg.option_type == option_type]
        if not candidates:
            return None

        scored: list[SelectedOption] = []
        for leg in candidates:
            if leg.delta is not None:
                delta, estimated = abs(leg.delta), False
            else:
                delta = compute_delta_from_price(
                    leg.ltp, spot, leg.strike_price, leg.expiry_epoch_seconds, option_type,
                )
                estimated = True
            scored.append(SelectedOption(
                symbol=leg.symbol, strike_price=leg.strike_price, option_type=option_type,
                ltp=leg.ltp, delta=delta, delta_is_estimated=estimated, fetched_at=time.time(),
            ))

        in_range = [s for s in scored if self.target_delta_min <= s.delta <= self.target_delta_max]
        if not in_range:
            logger.warning(
                "%s: no strike landed in delta range [%.2f, %.2f] (checked %d strikes).",
                option_type, self.target_delta_min, self.target_delta_max, len(scored),
            )
            return None

        midpoint = (self.target_delta_min + self.target_delta_max) / 2
        best = min(in_range, key=lambda s: abs(s.delta - midpoint))
        if best.delta_is_estimated:
            logger.info(
                "[DELTA_COMPUTED_LOCALLY] %s %s strike=%.0f delta=%.3f (Black-Scholes from live LTP)",
                option_type, best.symbol, best.strike_price, best.delta,
            )
        return best

    def select(self, expiry: str, current_spot: float, option_type: str) -> Optional[SelectedOption]:
        if self._cache_is_stale(current_spot):
            chain = self.adapter.get_option_chain(self.underlying_symbol, expiry)
            if not chain:
                return self._cache.get(option_type)  # fall back to stale cache over nothing
            for ot in ("CE", "PE"):
                selected = self._pick_by_delta(chain, ot, current_spot)
                if selected is not None:
                    self._cache[ot] = selected
            self._cache_spot_price = current_spot
            self._cache_fetched_at = time.time()
            logger.info(
                "Option chain cache refreshed at spot=%.2f: CE=%s PE=%s",
                current_spot,
                self._cache.get("CE").symbol if "CE" in self._cache else None,
                self._cache.get("PE").symbol if "PE" in self._cache else None,
            )
        return self._cache.get(option_type)
