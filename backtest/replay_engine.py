"""
backtest/replay_engine.py

Phase 9: replays a list of already-fetched historical candles (e.g. from
Historical Candle Data V3) through the EXACT SAME InstrumentStateMachine
used live — this is the whole point of a backtester: if strategy logic
ever needs a special "backtest mode" branch, the backtest stops proving
anything about the live code path.

This module does NOT fetch data itself (keeps it decoupled from the
Upstox REST client so it can also replay candles from a CSV/DB dump) —
callers hand it two lists of Candle objects (HTF, LTF) already in
chronological order.

Deliberately minimal for Phase 9: it drives the signal pipeline and
records every SignalDecision, but does not simulate order fills/slippage —
that would require modeling option-premium paths the spot-only candle
data here doesn't contain. Treat its output as "how many valid signals
would this ruleset have generated, and where" rather than a P&L backtest.
A premium-aware fill simulator is a reasonable next addition once Phase 6/7
are stable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from data_feed.candle_aggregator import Candle
from strategy.state_machine import InstrumentStateMachine, SignalDecision
from strategy.state_store import StateStore

logger = logging.getLogger(__name__)


@dataclass
class ReplayResult:
    instrument_key: str
    signals: list[SignalDecision]
    htf_candles_processed: int
    ltf_candles_processed: int


def replay(
    instrument_key: str,
    htf_candles: list[Candle],
    ltf_candles: list[Candle],
    session_config: dict,
    sweep_buffer_points: float,
    atr_multiplier: float = 1.5,
    state_path: str = ":memory:",
) -> ReplayResult:
    """`state_path=":memory:"` uses a throwaway temp file so repeated
    backtest runs never persist state into the live bot's state store."""
    import tempfile
    from pathlib import Path

    if state_path == ":memory:":
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        store = StateStore(Path(tmp.name))
    else:
        store = StateStore(Path(state_path))

    machine = InstrumentStateMachine(
        instrument_key=instrument_key,
        store=store,
        session_config=session_config,
        sweep_buffer_points=sweep_buffer_points,
        atr_multiplier=atr_multiplier,
    )

    # Merge HTF and LTF candles into one chronological stream, tagging each
    # so the machine gets on_htf_candle_close / on_ltf_candle_close calls in
    # true time order — otherwise the rolling base could lag or lead the
    # sweeps it's meant to gate.
    merged: list[tuple[Candle, str]] = (
        [(c, "htf") for c in htf_candles] + [(c, "ltf") for c in ltf_candles]
    )
    merged.sort(key=lambda pair: pair[0].open_time)

    signals: list[SignalDecision] = []
    for candle, kind in merged:
        if kind == "htf":
            machine.on_htf_candle_close(candle)
        else:
            decision = machine.on_ltf_candle_close(candle)
            if decision is not None:
                signals.append(decision)
                logger.info(
                    "REPLAY signal: %s %s at %s (spot_sl=%.2f)",
                    instrument_key, decision.direction,
                    decision.entry_candle.open_time, decision.spot_structural_sl,
                )

    return ReplayResult(
        instrument_key=instrument_key,
        signals=signals,
        htf_candles_processed=len(htf_candles),
        ltf_candles_processed=len(ltf_candles),
    )
