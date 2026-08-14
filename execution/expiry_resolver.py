"""
execution/expiry_resolver.py

Resolves the nearest tradeable expiry for an underlying via Fyers'
optionchain() call with strikecount=1 (cheapest possible request just to
read the expiry list — Fyers returns an `expiryData` list in the response
alongside the strikes). Cached per instrument per day, same rationale as
before: expiries don't change intraday.

CAVEAT: the exact response shape (`data.expiryData` as a list of
{'expiry': <epoch>, 'date': <str>}) is based on documented/community
examples, not a live call from this environment — no network path to
Fyers here. If `[No expiryData in option chain response]` shows up in
logs/bot.log, that's this assumption needing adjustment; share the
warning line (with the response dict) and it'll get fixed immediately.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)


class ExpiryResolver:
    def __init__(self, fyers_model):
        self.model = fyers_model
        self._cache: dict[str, tuple[date, str]] = {}  # underlying_symbol -> (cached_on, expiry_epoch_str)

    def nearest_expiry(self, underlying_symbol: str) -> Optional[str]:
        """Returns the expiry as the epoch-seconds string Fyers'
        optionchain() `timestamp` parameter expects (empty string means
        "current/nearest", which is actually the simplest correct answer —
        see fallback below)."""
        today = date.today()
        cached = self._cache.get(underlying_symbol)
        if cached is not None and cached[0] == today:
            return cached[1]

        try:
            response = self.model.optionchain(
                data={"symbol": underlying_symbol, "strikecount": 1, "timestamp": ""}
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to fetch option chain for %s", underlying_symbol)
            return None

        if not isinstance(response, dict) or response.get("s") != "ok":
            logger.error("Option chain fetch failed for %s: %s", underlying_symbol, response)
            return None

        expiry_data = (response.get("data") or {}).get("expiryData")
        if not expiry_data:
            logger.warning(
                "No expiryData in option chain response for %s — falling back to "
                "timestamp='' (Fyers' own 'nearest expiry' default). Response: %s",
                underlying_symbol, response,
            )
            self._cache[underlying_symbol] = (today, "")
            return ""

        # expiryData entries are typically {'expiry': <epoch str>, 'date': 'DDMMMYY'}.
        # Sort by epoch and take the nearest one that hasn't passed.
        try:
            sorted_entries = sorted(expiry_data, key=lambda e: int(e["expiry"]))
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "Unexpected expiryData shape for %s, using first entry as-is: %s",
                underlying_symbol, expiry_data,
            )
            sorted_entries = expiry_data

        nearest = sorted_entries[0]
        expiry_value = str(nearest.get("expiry", ""))
        self._cache[underlying_symbol] = (today, expiry_value)
        logger.info(
            "Resolved nearest expiry for %s: %s (%s)",
            underlying_symbol, expiry_value, nearest.get("date"),
        )
        return expiry_value
