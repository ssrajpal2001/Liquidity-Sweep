"""
execution/option_selector.py

Selects the ATM/ITM option contract (Delta target 0.50-0.60 per
config.option_selection) via Upstox's Put/Call Option Chain API, and
caches the result to avoid hitting that endpoint on every tick (Edge Case
4 from the review). Cache invalidates on a time interval OR when spot has
moved across a strike interval — whichever happens first.

Field names below (instrument_key, market_data.ask_price,
option_greeks.delta) are verified against the installed upstox_client
SDK's OptionStrikeData / PutCallOptionChainData / MarketData / AnalyticsData
classes (see swagger_types), not guessed.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import upstox_client
from upstox_client.rest import ApiException

logger = logging.getLogger(__name__)


@dataclass
class SelectedOption:
    instrument_key: str
    strike_price: float
    option_type: str  # "CE" or "PE"
    ask_price: float
    delta: float
    fetched_at: float  # time.time()


class OptionSelector:
    def __init__(
        self,
        api_client: "upstox_client.ApiClient",
        underlying_instrument_key: str,
        strike_interval: float,
        target_delta_min: float = 0.50,
        target_delta_max: float = 0.60,
        cache_refresh_seconds: int = 120,
    ):
        self.api = upstox_client.OptionsApi(api_client)
        self.underlying_instrument_key = underlying_instrument_key
        self.strike_interval = strike_interval
        self.target_delta_min = target_delta_min
        self.target_delta_max = target_delta_max
        self.cache_refresh_seconds = cache_refresh_seconds

        self._cache: dict[str, SelectedOption] = {}  # "CE"/"PE" -> SelectedOption
        self._cache_spot_price: Optional[float] = None
        self._cache_fetched_at: float = 0.0

    def _cache_is_stale(self, current_spot: float) -> bool:
        if not self._cache:
            return True
        if time.time() - self._cache_fetched_at > self.cache_refresh_seconds:
            return True
        if self._cache_spot_price is None:
            return True
        if abs(current_spot - self._cache_spot_price) >= self.strike_interval:
            return True
        return False

    def _fetch_chain(self, expiry_date: str) -> list["upstox_client.OptionStrikeData"]:
        try:
            response = self.api.get_put_call_option_chain(
                self.underlying_instrument_key, expiry_date
            )
        except ApiException as exc:
            logger.error("Option chain fetch failed: %s", exc)
            return []
        return getattr(response, "data", None) or []

    def _pick_by_delta(
        self, strikes: list["upstox_client.OptionStrikeData"], option_type: str
    ) -> Optional[SelectedOption]:
        best: Optional[SelectedOption] = None
        best_distance = float("inf")

        for strike in strikes:
            leg = strike.call_options if option_type == "CE" else strike.put_options
            if leg is None or leg.option_greeks is None or leg.market_data is None:
                continue
            delta = leg.option_greeks.delta
            if delta is None:
                continue
            abs_delta = abs(delta)

            if self.target_delta_min <= abs_delta <= self.target_delta_max:
                distance = abs(abs_delta - (self.target_delta_min + self.target_delta_max) / 2)
                if distance < best_distance:
                    best_distance = distance
                    best = SelectedOption(
                        instrument_key=leg.instrument_key,
                        strike_price=strike.strike_price,
                        option_type=option_type,
                        ask_price=leg.market_data.ask_price,
                        delta=delta,
                        fetched_at=time.time(),
                    )

        if best is None:
            logger.warning(
                "No %s strike found with |delta| in [%.2f, %.2f] — widening search may be needed.",
                option_type, self.target_delta_min, self.target_delta_max,
            )
        return best

    def select(
        self, expiry_date: str, current_spot: float, option_type: str
    ) -> Optional[SelectedOption]:
        """option_type: "CE" for a bullish sweep (call buy), "PE" for a
        bearish sweep (put buy). Returns the cached selection unless the
        cache is stale (Edge Case 4)."""
        if self._cache_is_stale(current_spot):
            strikes = self._fetch_chain(expiry_date)
            if not strikes:
                return self._cache.get(option_type)  # fall back to stale cache over nothing
            for ot in ("CE", "PE"):
                selected = self._pick_by_delta(strikes, ot)
                if selected is not None:
                    self._cache[ot] = selected
            self._cache_spot_price = current_spot
            self._cache_fetched_at = time.time()
            logger.info(
                "Option chain cache refreshed at spot=%.2f: CE=%s PE=%s",
                current_spot,
                self._cache.get("CE").instrument_key if "CE" in self._cache else None,
                self._cache.get("PE").instrument_key if "PE" in self._cache else None,
            )

        return self._cache.get(option_type)
