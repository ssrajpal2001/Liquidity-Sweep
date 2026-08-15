"""
strategy/state_machine.py

Coordinates the full signal pipeline per instrument and owns the
Void/Invalidation state — this is Gap 3 from the architecture review made
concrete: BLOCKED clears via an explicit, coded condition (price retests
the originally swept level, OR the rolling base engine establishes a new
base), never left as an unreachable dead end.

Pipeline per LTF candle close, mirroring workflow_diagram.mermaid:

    void state?
      BLOCKED -> check reset condition -> if reset: CLEAR, else: stop here
      CLEAR   -> continue

    sweep_detector.detect_sweep(candle, base.high, base.low)
      no sweep -> stop here
      sweep    -> continue

    displacement.detect_displacement(...)
      no displacement -> stop here (does NOT set BLOCKED — only a filled/
                          missed SL sets BLOCKED; a sweep that never
                          displaces just wasn't a valid setup)
      displacement     -> continue

    displacement.detect_fvg(...)
      no FVG -> stop here
      FVG    -> retest_trigger.arm(fvg)

    (on later candles) retest_trigger.check(candle)
      no retest yet -> stop here
      retest fires  -> filters.is_within_trading_window / is_bias_aligned
                        both pass -> emit a SignalDecision the caller can
                        hand to execution/option_selector.py
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from data_feed.candle_aggregator import Candle
from strategy import displacement as displacement_mod
from strategy.filters import Bias, htf_bias_from_base, is_bias_aligned, is_within_trading_window
from strategy.retest_trigger import RetestWatcher
from strategy.rolling_base import RollingBaseEngine
from strategy.state_store import StateStore
from strategy.sweep_detector import SweepEvent, detect_sweep

logger = logging.getLogger(__name__)


@dataclass
class SignalDecision:
    instrument_key: str
    direction: str  # "bearish" (PUT) or "bullish" (CALL)
    sweep: SweepEvent
    fvg: displacement_mod.FairValueGap
    entry_candle: Candle
    spot_structural_sl: float  # sweep candle's extreme + buffer, per risk.sweep_buffer_points


class InstrumentStateMachine:
    def __init__(
        self,
        instrument_key: str,
        store: StateStore,
        session_config: dict,
        sweep_buffer_points: float,
        atr_multiplier: float = 1.5,
        on_event: Optional[Callable[[str, dict], None]] = None,
    ):
        self.instrument_key = instrument_key
        self.store = store
        self.session_config = session_config
        self.sweep_buffer_points = sweep_buffer_points
        self.atr_multiplier = atr_multiplier
        # Optional diagnostic hook — default no-op, zero effect on live
        # trading behavior. Fires at every pipeline stage (rolling base
        # update, sweep detected/rejected, displacement, FVG, retest
        # armed/triggered/stale, signal filtered/passed) so a backtest
        # reporting layer can build a full audit trail without needing to
        # duplicate or monkeypatch this class's internals.
        self._on_event = on_event or (lambda event, data: None)

        self.rolling_base = RollingBaseEngine(instrument_key, store)
        self.retest = RetestWatcher()

        self._ltf_history: list[Candle] = []      # recent LTF candles, for ATR + micro-swing
        self._pending_sweep: Optional[SweepEvent] = None

    # -- void state ------------------------------------------------------------
    def _is_blocked(self) -> bool:
        return self.store.get(self.instrument_key).void_blocked

    def _set_blocked(self, level: float) -> None:
        state = self.store.get(self.instrument_key)
        state.void_blocked = True
        state.void_blocked_zone_level = level
        self.store.save(state)
        logger.warning("%s: void state set to BLOCKED at level %.2f", self.instrument_key, level)

    def _check_void_reset(self, candle: Candle) -> None:
        """Explicit reset condition (Gap 3 fix): clears when price retests
        the originally swept level, OR when the rolling base engine
        establishes a new base (checked separately in on_htf_candle_close)."""
        state = self.store.get(self.instrument_key)
        if not state.void_blocked:
            return
        level = state.void_blocked_zone_level
        if level is None:
            return
        retested = candle.low <= level <= candle.high
        if retested:
            state.void_blocked = False
            state.void_blocked_zone_level = None
            self.store.save(state)
            logger.info(
                "%s: void state CLEARED — price retested swept level %.2f",
                self.instrument_key, level,
            )

    # -- HTF feed (75m closes) --------------------------------------------------
    def on_htf_candle_close(self, candle: Candle) -> None:
        new_base_established = self.rolling_base.on_htf_candle_close(candle)
        if new_base_established:
            base = self.rolling_base.current_base
            self._on_event("rolling_base_updated", {
                "candle_open_time": candle.open_time, "base_high": base.high, "base_low": base.low,
            })
            state = self.store.get(self.instrument_key)
            if state.void_blocked:
                state.void_blocked = False
                state.void_blocked_zone_level = None
                self.store.save(state)
                logger.info(
                    "%s: void state CLEARED — new rolling base supersedes the old level",
                    self.instrument_key,
                )

    # -- LTF feed (3m/5m closes) — the main signal pipeline ---------------------
    def on_ltf_candle_close(self, candle: Candle) -> Optional[SignalDecision]:
        self._ltf_history.append(candle)
        self._ltf_history = self._ltf_history[-30:]  # bounded lookback

        self._check_void_reset(candle)

        # 1. Retest watcher takes priority if already armed from a prior candle.
        if self.retest.is_armed:
            was_armed_before = self.retest.is_armed
            triggered = self.retest.check(candle)
            if triggered:
                self._on_event("retest_triggered", {"candle_open_time": candle.open_time})
                return self._finalize_signal(candle)
            if was_armed_before and not self.retest.is_armed:
                # check() disarmed it internally (staleness) without triggering.
                self._on_event("retest_stale", {"candle_open_time": candle.open_time})
            return None

        if self._is_blocked():
            return None

        base = self.rolling_base.current_base
        if base is None:
            return None

        # 2. Sweep detection against the current (possibly just-updated) base.
        sweep = detect_sweep(candle, base.high, base.low)
        if sweep is None:
            return None
        logger.info("%s: sweep detected — %s", self.instrument_key, sweep)
        self._on_event("sweep_detected", {
            "candle_open_time": candle.open_time, "direction": sweep.direction.value,
            "level": sweep.level, "pierce_points": sweep.pierce_points,
        })
        self._pending_sweep = sweep

        # 3. Displacement / MSS confirmation on this same candle (the sweep
        #    candle itself, if it also displaces — otherwise wait; a fuller
        #    implementation would check the next 1-2 candles too).
        lookback = self._ltf_history[-6:-1]
        recent_swing_high = max((c.high for c in self._ltf_history[-6:-1]), default=candle.high)
        recent_swing_low = min((c.low for c in self._ltf_history[-6:-1]), default=candle.low)
        disp = displacement_mod.detect_displacement(
            candle, lookback, recent_swing_high, recent_swing_low, self.atr_multiplier
        )
        if disp is None:
            self._on_event("sweep_rejected", {
                "candle_open_time": candle.open_time, "reason": "no_displacement",
            })
            self._pending_sweep = None
            return None
        logger.info("%s: displacement confirmed — %s", self.instrument_key, disp)
        self._on_event("displacement_confirmed", {
            "candle_open_time": candle.open_time, "direction": disp.direction.value,
            "body_to_atr_ratio": disp.body_to_atr_ratio,
        })

        # 4. FVG check needs the candle after the displacement candle, which
        #    we don't have yet on this call — arm the retest watcher only
        #    once we can compute the FVG on the next candle.
        if len(self._ltf_history) >= 3:
            fvg = displacement_mod.detect_fvg(
                self._ltf_history[-3], self._ltf_history[-2], self._ltf_history[-1]
            )
            if fvg is not None:
                self.retest.arm(fvg)
                self._on_event("retest_armed", {
                    "candle_open_time": candle.open_time, "gap_low": fvg.gap_low, "gap_high": fvg.gap_high,
                })
            else:
                self._on_event("sweep_rejected", {
                    "candle_open_time": candle.open_time, "reason": "no_fvg",
                })
                self._pending_sweep = None
        return None

    def _finalize_signal(self, entry_candle: Candle) -> Optional[SignalDecision]:
        sweep = self._pending_sweep
        fvg = self.retest._active.fvg if self.retest._active else None  # noqa: SLF001
        self._pending_sweep = None
        if sweep is None or fvg is None:
            logger.error(
                "%s: retest fired without a matching sweep/FVG context — discarding.",
                self.instrument_key,
            )
            return None

        # Use the candle's OWN timestamp, not wall-clock time — using
        # datetime.now() here was a real bug: it made the time-window
        # filter check the moment the backtest was RUN, not the moment
        # the signal actually occurred in history, silently discarding
        # every backtested signal regardless of its real historical time.
        # For live trading this is a no-op change (a live candle's
        # open_time is already effectively "now").
        window_ok, window_reason = is_within_trading_window(entry_candle.open_time, self.session_config)
        if not window_ok:
            logger.info("%s: signal discarded — %s", self.instrument_key, window_reason)
            self._on_event("signal_filtered", {
                "candle_open_time": entry_candle.open_time, "reason": window_reason,
            })
            return None

        base = self.rolling_base.current_base
        if base is None:
            return None
        bias = htf_bias_from_base(base.high, base.low, entry_candle.close)
        bias_ok, bias_reason = is_bias_aligned(sweep.direction.value, bias)
        if not bias_ok:
            logger.info("%s: signal discarded — %s", self.instrument_key, bias_reason)
            self._on_event("signal_filtered", {
                "candle_open_time": entry_candle.open_time, "reason": bias_reason,
            })
            return None

        logger.info("%s: signal PASSED all filters — %s", self.instrument_key, bias_reason)

        if sweep.direction.value == "bearish":
            spot_sl = sweep.candle.high + self.sweep_buffer_points
        else:
            spot_sl = sweep.candle.low - self.sweep_buffer_points

        self._on_event("signal_passed", {
            "candle_open_time": entry_candle.open_time, "direction": sweep.direction.value,
            "spot_sl": spot_sl,
        })

        return SignalDecision(
            instrument_key=self.instrument_key,
            direction=sweep.direction.value,
            sweep=sweep,
            fvg=fvg,
            entry_candle=entry_candle,
            spot_structural_sl=spot_sl,
        )

    def on_sl_hit(self, level: float) -> None:
        """Call this from execution/order_manager.py when a position's SL
        is hit — sets void BLOCKED on that zone so the bot doesn't
        immediately re-enter the same failed setup (Phase 7 wiring)."""
        self._set_blocked(level)
