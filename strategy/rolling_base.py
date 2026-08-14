"""
strategy/rolling_base.py

Dynamic Rolling Base engine (75-min HTF, per config). This is a genuine
feedback loop, not a one-time calculation, per the gap fixed in the
approved architecture: every time a new 75m candle CLOSES THROUGH the
current base, a new base is established and persisted — every downstream
check (sweep detector, void-state) reads whatever the base currently is,
not a value computed once at startup.

Definition used here (matches the discussion): the base is the most
recent 75m candle whose close broke past the *previous* 75m candle's
range in the direction of the prevailing move — i.e. any 75m candle
closing below the previous candle's low establishes a new bearish base
(base_high/base_low = that candle's high/low), and symmetrically for a
close above the previous candle's high on the bullish side. Until a break
happens, the base stays exactly where it is.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from data_feed.candle_aggregator import Candle
from strategy.state_store import InstrumentState, StateStore

logger = logging.getLogger(__name__)


@dataclass
class RollingBase:
    high: float
    low: float
    established_at: str  # ISO 8601 open_time of the candle that set this base


class RollingBaseEngine:
    """One instance handles one instrument's 75m rolling base."""

    def __init__(self, instrument_key: str, store: StateStore):
        self.instrument_key = instrument_key
        self.store = store
        self._prev_candle: Optional[Candle] = None

        state = store.get(instrument_key)
        if state.rolling_base_high is not None and state.rolling_base_low is not None:
            self._base = RollingBase(
                high=state.rolling_base_high,
                low=state.rolling_base_low,
                established_at=state.rolling_base_candle_open_time or "",
            )
            logger.info(
                "%s: restored rolling base from state store: high=%.2f low=%.2f",
                instrument_key, self._base.high, self._base.low,
            )
        else:
            self._base: Optional[RollingBase] = None

    @property
    def current_base(self) -> Optional[RollingBase]:
        return self._base

    def on_htf_candle_close(self, candle: Candle) -> bool:
        """Feed each newly-closed 75m candle here. Returns True if this
        candle established a NEW base (the feedback-loop event the
        original flowchart was missing)."""
        assert candle.timeframe_minutes == 75, "RollingBaseEngine expects 75-minute candles"

        base_updated = False

        if self._prev_candle is not None:
            broke_down = candle.close < self._prev_candle.low
            broke_up = candle.close > self._prev_candle.high

            if broke_down or broke_up:
                self._base = RollingBase(
                    high=candle.high,
                    low=candle.low,
                    established_at=candle.open_time.isoformat(),
                )
                base_updated = True
                logger.info(
                    "%s: NEW rolling base established (%s break) at %s — high=%.2f low=%.2f",
                    self.instrument_key,
                    "bearish" if broke_down else "bullish",
                    candle.open_time,
                    self._base.high, self._base.low,
                )
                self._persist()

        self._prev_candle = candle
        return base_updated

    def _persist(self) -> None:
        state = self.store.get(self.instrument_key)
        state.rolling_base_high = self._base.high
        state.rolling_base_low = self._base.low
        state.rolling_base_candle_open_time = self._base.established_at
        self.store.save(state)

    def is_swept(self, ltf_candle: Candle) -> Optional[str]:
        """True sweep-candidate check against the CURRENT base (whatever it
        is right now — this is what makes the feedback loop matter: after a
        base update, this immediately checks against the new level, not a
        stale one). Returns 'bearish', 'bullish', or None.

        Bearish sweep candidate: LTF high pierces base.high, close comes
        back inside (< base.high) -> potential PUT setup.
        Bullish sweep candidate: LTF low pierces base.low, close comes
        back inside (> base.low) -> potential CALL setup.
        """
        if self._base is None:
            return None
        if ltf_candle.high > self._base.high and ltf_candle.close < self._base.high:
            return "bearish"
        if ltf_candle.low < self._base.low and ltf_candle.close > self._base.low:
            return "bullish"
        return None
