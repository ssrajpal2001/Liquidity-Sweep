"""
execution/option_selector.py

Selects the option contract to trade via Fyers' optionchain() API, with
1-3 min caching (Edge Case 4 from the review) exactly as before.

DELTA FIX: this used to fall back to a hardcoded ASSUMED_ATM_DELTA
constant when a broker's response had no delta field, which is a weak
guess. It now falls back to execution/greeks_engine.py's broker-
independent Black-Scholes calculation instead — computed from the
option's own live LTP, spot, strike, and time-to-expiry, so it never
depends on whether Fyers (or any future broker) happens to supply
Greeks. Whether a leg's delta came from the broker or was computed is
tracked on SelectedOption.delta_is_estimated and logged either way.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from execution.greeks_engine import compute_delta_from_price

logger = logging.getLogger(__name__)


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

    def _pick_by_delta(self, chain: list[dict], option_type: str, spot: float, expiry: str) -> Optional[SelectedOption]:
        candidates = [row for row in chain if row.get("option_type") == option_type]
        if not candidates:
            return None

        # Compute (or read) a delta for every candidate strike, then pick
        # whichever lands closest to the target range midpoint — same
        # selection logic regardless of where the delta came from.
        scored: list[SelectedOption] = []
        for row in candidates:
            try:
                strike = float(row["strike_price"])
                ltp = float(row.get("ltp", 0))
                symbol = row["symbol"]
            except (KeyError, TypeError, ValueError):
                continue

            raw_delta = row.get("delta")
            if raw_delta not in (None, "", 0):
                try:
                    delta = abs(float(raw_delta))
                    estimated = False
                except (TypeError, ValueError):
                    delta, estimated = self._estimate_delta(ltp, spot, strike, expiry, option_type)
            else:
                delta, estimated = self._estimate_delta(ltp, spot, strike, expiry, option_type)

            scored.append(SelectedOption(
                symbol=symbol, strike_price=strike, option_type=option_type,
                ltp=ltp, delta=delta, delta_is_estimated=estimated, fetched_at=time.time(),
            ))

        in_range = [s for s in scored if self.target_delta_min <= s.delta <= self.target_delta_max]
        if not in_range:
            logger.warning(
                "%s: no strike landed in delta range [%.2f, %.2f] (checked %d strikes, "
                "%d with broker-supplied delta) — widening search may be needed.",
                option_type, self.target_delta_min, self.target_delta_max,
                len(scored), sum(1 for s in scored if not s.delta_is_estimated),
            )
            return None

        midpoint = (self.target_delta_min + self.target_delta_max) / 2
        best = min(in_range, key=lambda s: abs(s.delta - midpoint))
        if best.delta_is_estimated:
            logger.info(
                "[DELTA_COMPUTED_LOCALLY] %s %s strike=%.0f delta=%.3f (Black-Scholes from live LTP, "
                "not broker-supplied)", option_type, best.symbol, best.strike_price, best.delta,
            )
        return best

    @staticmethod
    def _estimate_delta(
        ltp: float, spot: float, strike: float, expiry: str, option_type: str
    ) -> tuple[float, bool]:
        try:
            expiry_epoch = float(expiry) if expiry else None
        except (TypeError, ValueError):
            expiry_epoch = None
        delta = compute_delta_from_price(ltp, spot, strike, expiry_epoch, option_type)
        return delta, True

    def select(self, expiry: str, current_spot: float, option_type: str) -> Optional[SelectedOption]:
        if self._cache_is_stale(current_spot):
            chain = self._fetch_chain(expiry)
            if not chain:
                return self._cache.get(option_type)  # fall back to stale cache over nothing
            for ot in ("CE", "PE"):
                selected = self._pick_by_delta(chain, ot, current_spot, expiry)
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
