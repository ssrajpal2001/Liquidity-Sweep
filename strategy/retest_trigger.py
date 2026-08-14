"""
strategy/retest_trigger.py

This is the module that closes Gap 1 from the architecture review: entries
fire on a PULLBACK into the fresh FVG/order block, never on the
displacement candle itself. Once displacement.detect_fvg() confirms a
fresh FVG, this module arms a watcher for that specific zone; every
subsequent LTF candle is checked against it until either:
  - price retests into the zone -> entry trigger fires, or
  - the zone becomes stale (see STALE_AFTER_CANDLES) -> disarmed, no entry.

This is intentionally a small, single-purpose state object (not folded
into the bigger void/invalidation state machine) so "waiting for a
retest" and "blocked after a failed sweep" stay independently reasoned
about, even though strategy/state_machine.py coordinates both.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from data_feed.candle_aggregator import Candle
from strategy.displacement import DisplacementDirection, FairValueGap

logger = logging.getLogger(__name__)

# If price hasn't retested within this many LTF candles, the zone is
# considered stale — re-entering on an old FVG risks trading a level the
# market has already moved past.
STALE_AFTER_CANDLES = 12


@dataclass
class RetestTrigger:
    fvg: FairValueGap
    candles_waited: int = 0
    armed: bool = True


class RetestWatcher:
    def __init__(self):
        self._active: Optional[RetestTrigger] = None

    @property
    def is_armed(self) -> bool:
        return self._active is not None and self._active.armed

    def arm(self, fvg: FairValueGap) -> None:
        self._active = RetestTrigger(fvg=fvg)
        logger.info(
            "Retest armed: %s FVG [%.2f, %.2f] formed at %s",
            fvg.direction.value, fvg.gap_low, fvg.gap_high, fvg.formed_at_candle_open_time,
        )

    def disarm(self, reason: str) -> None:
        if self._active is not None:
            logger.info("Retest disarmed: %s", reason)
        self._active = None

    def check(self, candle: Candle) -> bool:
        """Feed each new LTF candle. Returns True exactly once, on the
        candle whose range pulls back into the armed FVG zone — that True
        is the entry trigger for execution/option_selector.py onward.
        Returns False (and may auto-disarm on staleness) otherwise."""
        if self._active is None or not self._active.armed:
            return False

        self._active.candles_waited += 1
        if self._active.candles_waited > STALE_AFTER_CANDLES:
            self.disarm(f"stale after {STALE_AFTER_CANDLES} candles with no retest")
            return False

        fvg = self._active.fvg
        retested = candle.low <= fvg.gap_high and candle.high >= fvg.gap_low
        if retested:
            logger.info(
                "Retest triggered: %s candle [%.2f, %.2f] entered FVG [%.2f, %.2f]",
                candle.open_time, candle.low, candle.high, fvg.gap_low, fvg.gap_high,
            )
            self._active.armed = False
            return True
        return False
