"""
execution/angelone_instrument_master.py

Downloads and caches AngelOne's public instrument/scrip master file —
the ONLY way to resolve a strike/expiry to the numeric `symboltoken`
AngelOne's LTP, option-chain, and order-placement APIs all require.
Schema verified against multiple independent, real forum-posted samples
(not guessed), consistent across 6+ separate sources:

    {"token":"58784","symbol":"NIFTY28OCT2524400CE","name":"NIFTY",
     "expiry":"28OCT2025","strike":"2440000.000000","lotsize":"75",
     "instrumenttype":"OPTIDX","exch_seg":"NFO","tick_size":"5.000000"}

Two quirks confirmed from multiple independent posts, not assumed:
  - `strike` is the real strike x 100 (24400 -> "2440000.000000") — this
    module divides by 100 on load so callers work in real strike values.
  - Index SPOT tokens (bare NIFTY/BANKNIFTY, not futures/options) are NOT
    in this file at all (confirmed by multiple forum threads — AngelOne
    has even changed/removed these before). spot_index_token() below is
    NOT independently verified against a live account — confirm before
    relying on it.

The file is tens of MB and doesn't change intraday, so it's downloaded
once per day and cached locally.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
CACHE_PATH = Path("secrets/angelone_scrip_master_cache.json")
CACHE_MAX_AGE_HOURS = 20
OPTION_INSTRUMENT_TYPES = ("OPTIDX", "OPTSTK")

# NOT independently verified here (no live account to test against), and
# AngelOne has changed these before (e.g. a NIFTY -> NIFTY50 symbol
# rename that broke this exact lookup for others). Confirm against your
# account — e.g. via searchScrip — before trusting these for real orders.
KNOWN_INDEX_SPOT_TOKENS: dict[tuple[str, str], str] = {
    ("NIFTY", "NSE"): "99926000",
    ("SENSEX", "BSE"): "99919000",
}


@dataclass
class ScripEntry:
    token: str
    symbol: str
    name: str
    expiry: Optional[date]
    strike: float  # real strike value, already divided by 100
    lotsize: int
    instrumenttype: str
    exch_seg: str


class AngelOneInstrumentMaster:
    def __init__(self, cache_path: Path = CACHE_PATH):
        self.cache_path = Path(cache_path)
        self._entries: Optional[list[ScripEntry]] = None

    def _load(self) -> list[ScripEntry]:
        if self._entries is not None:
            return self._entries

        raw = self._load_cached_or_download()
        entries: list[ScripEntry] = []
        for row in raw:
            try:
                strike_raw = float(row.get("strike", "-1"))
                strike = strike_raw / 100 if strike_raw > 0 else -1.0
                expiry_dt = None
                expiry_str = row.get("expiry", "")
                if expiry_str:
                    try:
                        expiry_dt = datetime.strptime(expiry_str, "%d%b%Y").date()
                    except ValueError:
                        expiry_dt = None
                entries.append(ScripEntry(
                    token=row["token"], symbol=row["symbol"], name=row.get("name", ""),
                    expiry=expiry_dt, strike=strike,
                    lotsize=int(row.get("lotsize", 1) or 1),
                    instrumenttype=row.get("instrumenttype", ""),
                    exch_seg=row.get("exch_seg", ""),
                ))
            except (KeyError, TypeError, ValueError):
                continue

        self._entries = entries
        logger.info("Loaded %d AngelOne instrument master entries.", len(entries))
        return entries

    def _load_cached_or_download(self) -> list[dict]:
        if self.cache_path.exists():
            age_hours = (time.time() - self.cache_path.stat().st_mtime) / 3600
            if age_hours < CACHE_MAX_AGE_HOURS:
                try:
                    return json.loads(self.cache_path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    logger.warning("Cached AngelOne scrip master unreadable, re-downloading.")

        logger.info("Downloading AngelOne instrument master (large file, once per day)...")
        resp = requests.get(SCRIP_MASTER_URL, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(data), encoding="utf-8")
        return data

    def option_expiries(self, name: str, exch_seg: str = "NFO") -> list[date]:
        entries = self._load()
        expiries = {
            e.expiry for e in entries
            if e.name == name and e.exch_seg == exch_seg
            and e.instrumenttype in OPTION_INSTRUMENT_TYPES and e.expiry is not None
        }
        return sorted(expiries)

    def nearest_option_expiry(self, name: str, exch_seg: str = "NFO", today: Optional[date] = None) -> Optional[date]:
        today = today or date.today()
        future = [e for e in self.option_expiries(name, exch_seg) if e >= today]
        return future[0] if future else None

    def strikes_for_expiry(self, name: str, expiry: date, exch_seg: str = "NFO") -> list[ScripEntry]:
        entries = self._load()
        return [
            e for e in entries
            if e.name == name and e.exch_seg == exch_seg and e.expiry == expiry
            and e.instrumenttype in OPTION_INSTRUMENT_TYPES
        ]

    def find_by_symbol(self, symbol: str) -> Optional[ScripEntry]:
        entries = self._load()
        for e in entries:
            if e.symbol == symbol:
                return e
        return None

    @staticmethod
    def spot_index_token(name: str, exch: str = "NSE") -> Optional[str]:
        return KNOWN_INDEX_SPOT_TOKENS.get((name, exch))
