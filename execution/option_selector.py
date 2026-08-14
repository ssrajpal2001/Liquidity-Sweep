"""
execution/option_selector.py

Selects the option contract to trade via Fyers' optionchain() API, with
1-3 min caching (Edge Case 4 from the review) exactly as before.

IMPORTANT CAVEAT, found during research for this integration: multiple
Fyers community threads (spanning 2024-2026) ask whether the option chain
API reliably returns Greeks/Delta at all — this is NOT settled the way
Upstox's typed SDK response was. So this module tries the documented
`greeks=1` path first, but if no strike in the response actually carries
a usable delta field, it falls back to selecting by STRIKE DISTANCE from
spot (nearest ATM, or N strikes ITM) rather than crashing or silently
picking a wrong contract. Which path was used is logged explicitly —
please check for `[DELTA_UNAVAILABLE_FALLBACK]` in tomorrow's log; if it
fires, the "Delta ≈ 0.50-0.60" selection logic is running in degraded
mode and the risk math (Spot Risk x Delta = Premium SL) will use an
assumed delta rather than a live one.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

ASSUMED_ATM_DELTA = 0.50  # used only in the fallback path, see module docstring


@dataclass
class SelectedOption:
    symbol: str
    strike_price: float
    option_type: str  # "CE" or "PE"
    ltp: float
    delta: float
    delta_is_estimated: bool
    fetched_at: float


class OptionSelector:
    def __init__(
        self,
        fyers_model,
        underlying_symbol: str,
        strike_interval: float,
        target_delta_min: float = 0.50,
        target_delta_max: float = 0.60,
        cache_refresh_seconds: int = 120,
        strike_count: int = 10,
    ):
        self.model = fyers_model
        self.underlying_symbol = underlying_symbol
        self.strike_interval = strike_interval
        self.target_delta_min = target_delta_min
        self.target_delta_max = target_delta_max
        self.cache_refresh_seconds = cache_refresh_seconds
        self.strike_count = strike_count

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
        if abs(current_spot - self._cache_spot_price) >= self.strike_interval:
            return True
        return False

    def _fetch_chain(self, expiry: str) -> list[dict]:
        try:
            response = self.model.optionchain(
                data={
                    "symbol": self.underlying_symbol,
                    "strikecount": self.strike_count,
                    "timestamp": expiry,
                    "greeks": "1",
                }
            )
        except Exception:  # noqa: BLE001
            logger.exception("Option chain fetch failed for %s", self.underlying_symbol)
            return []

        if not isinstance(response, dict) or response.get("s") != "ok":
            logger.error("Option chain fetch failed for %s: %s", self.underlying_symbol, response)
            return []

        chain = (response.get("data") or {}).get("optionsChain") or []
        return [row for row in chain if row.get("option_type") in ("CE", "PE")]

    def _pick_by_delta(self, chain: list[dict], option_type: str, spot: float) -> Optional[SelectedOption]:
        candidates = [row for row in chain if row.get("option_type") == option_type]
        if not candidates:
            return None

        # Path 1: real delta from the response, if present.
        with_delta = [c for c in candidates if c.get("delta") not in (None, "", 0)]
        if with_delta:
            best, best_distance = None, float("inf")
            midpoint = (self.target_delta_min + self.target_delta_max) / 2
            for row in with_delta:
                try:
                    abs_delta = abs(float(row["delta"]))
                except (TypeError, ValueError):
                    continue
                if self.target_delta_min <= abs_delta <= self.target_delta_max:
                    distance = abs(abs_delta - midpoint)
                    if distance < best_distance:
                        best_distance = distance
                        best = SelectedOption(
                            symbol=row["symbol"], strike_price=float(row["strike_price"]),
                            option_type=option_type, ltp=float(row.get("ltp", 0)),
                            delta=abs_delta, delta_is_estimated=False, fetched_at=time.time(),
                        )
            if best is not None:
                return best
            logger.warning(
                "%s: response had delta values but none in [%.2f, %.2f] — "
                "falling back to strike-distance selection.",
                option_type, self.target_delta_min, self.target_delta_max,
            )

        # Path 2: fallback — no usable delta anywhere in the response.
        logger.warning(
            "[DELTA_UNAVAILABLE_FALLBACK] %s: no delta field in option chain response "
            "for %s — selecting by strike distance from spot instead (assumed delta=%.2f).",
            option_type, self.underlying_symbol, ASSUMED_ATM_DELTA,
        )
        candidates_sorted = sorted(candidates, key=lambda r: abs(float(r["strike_price"]) - spot))
        if not candidates_sorted:
            return None
        row = candidates_sorted[0]  # nearest-to-spot strike, i.e. ATM
        return SelectedOption(
            symbol=row["symbol"], strike_price=float(row["strike_price"]),
            option_type=option_type, ltp=float(row.get("ltp", 0)),
            delta=ASSUMED_ATM_DELTA, delta_is_estimated=True, fetched_at=time.time(),
        )

    def select(self, expiry: str, current_spot: float, option_type: str) -> Optional[SelectedOption]:
        if self._cache_is_stale(current_spot):
            chain = self._fetch_chain(expiry)
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
