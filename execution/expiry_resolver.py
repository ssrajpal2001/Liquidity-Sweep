"""
execution/expiry_resolver.py

Resolves the nearest tradeable weekly expiry for an underlying by querying
Upstox's actual contract list (OptionsApi.get_option_contracts), NOT by
hardcoding a weekday. This matters because NSE has changed which weekday
Nifty/Sensex weekly options expire on more than once — hardcoding
"Tuesday" or "Thursday" is exactly the kind of assumption that silently
goes stale. InstrumentData.expiry / .weekly are verified fields on the
installed SDK.

Cached per instrument for the trading day — expiries don't change
intraday, so there's no need to hit this endpoint more than once per
instrument per day.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import upstox_client
from upstox_client.rest import ApiException

logger = logging.getLogger(__name__)


class ExpiryResolver:
    def __init__(self, api_client: "upstox_client.ApiClient"):
        self.api = upstox_client.OptionsApi(api_client)
        self._cache: dict[str, tuple[date, str]] = {}  # underlying_key -> (cached_on, expiry_str)

    def nearest_weekly_expiry(self, underlying_key: str) -> Optional[str]:
        today = date.today()
        cached = self._cache.get(underlying_key)
        if cached is not None and cached[0] == today:
            return cached[1]

        try:
            response = self.api.get_option_contracts(underlying_key)
        except ApiException as exc:
            logger.error("Failed to fetch option contracts for %s: %s", underlying_key, exc)
            return None

        contracts = getattr(response, "data", None) or []
        weekly_expiries = sorted(
            {
                c.expiry.date() if isinstance(c.expiry, datetime) else c.expiry
                for c in contracts
                if getattr(c, "weekly", False) and c.expiry is not None
            }
        )
        future_expiries = [e for e in weekly_expiries if e >= today]
        if not future_expiries:
            logger.error(
                "No future weekly expiries found for %s (got %d contracts total).",
                underlying_key, len(contracts),
            )
            return None

        nearest = future_expiries[0]
        expiry_str = nearest.strftime("%Y-%m-%d")
        self._cache[underlying_key] = (today, expiry_str)
        logger.info("Resolved nearest weekly expiry for %s: %s", underlying_key, expiry_str)
        return expiry_str
