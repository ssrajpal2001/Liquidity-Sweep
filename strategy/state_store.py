"""
strategy/state_store.py

Persists per-instrument strategy state (current rolling base, void/
invalidation flag, retest-armed flag) to a local JSON file, so a process
restart mid-session doesn't forget the current base and re-derive a wrong
one, and doesn't forget a BLOCKED void-state and immediately re-enter a
zone that just failed.

JSON file rather than SQLite/Redis for Phase 3: state here is small
(one record per instrument) and read/written far less often than ticks —
a file with atomic writes is simpler to reason about and to inspect by
hand (`cat state/strategy_state.json`) during the sandbox test tomorrow.
Swapping to SQLite/Redis later only requires changing this one module.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path("state/strategy_state.json")


@dataclass
class InstrumentState:
    instrument_key: str
    rolling_base_high: Optional[float] = None
    rolling_base_low: Optional[float] = None
    rolling_base_candle_open_time: Optional[str] = None  # ISO 8601
    void_blocked: bool = False
    void_blocked_zone_level: Optional[float] = None
    retest_armed: bool = False
    retest_zone_high: Optional[float] = None
    retest_zone_low: Optional[float] = None
    updated_at: Optional[str] = None


class StateStore:
    def __init__(self, path: Path | str = DEFAULT_STATE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, InstrumentState] = self._load()

    def _load(self) -> dict[str, InstrumentState]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {k: InstrumentState(**v) for k, v in raw.items()}
        except Exception:  # noqa: BLE001
            logger.exception(
                "Could not read state file at %s — starting with empty state. "
                "If this happens on a live server, check the file for corruption.",
                self.path,
            )
            return {}

    def _save(self) -> None:
        # Atomic write: write to a temp file then rename, so a crash
        # mid-write never leaves a half-written, unparseable state file.
        data = {k: asdict(v) for k, v in self._state.items()}
        fd, tmp_path = tempfile.mkstemp(dir=self.path.parent, prefix=".tmp_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self.path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def get(self, instrument_key: str) -> InstrumentState:
        if instrument_key not in self._state:
            self._state[instrument_key] = InstrumentState(instrument_key=instrument_key)
        return self._state[instrument_key]

    def save(self, state: InstrumentState) -> None:
        from datetime import datetime, timezone, timedelta

        IST = timezone(timedelta(hours=5, minutes=30))
        state.updated_at = datetime.now(IST).isoformat()
        self._state[state.instrument_key] = state
        self._save()

    def all(self) -> dict[str, InstrumentState]:
        return dict(self._state)
