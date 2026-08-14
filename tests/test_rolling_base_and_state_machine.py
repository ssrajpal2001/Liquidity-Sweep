from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from data_feed.candle_aggregator import Candle
from strategy.rolling_base import RollingBaseEngine
from strategy.state_machine import InstrumentStateMachine
from strategy.state_store import StateStore


def _htf_candle(o, h, l, c, hour=9, minute=15):
    return Candle(
        instrument_key="NIFTY", timeframe_minutes=75,
        open_time=datetime(2026, 8, 14, hour, minute, tzinfo=timezone.utc),
        open=o, high=h, low=l, close=c,
    )


def _ltf_candle(o, h, l, c, hour=9, minute=15):
    return Candle(
        instrument_key="NIFTY", timeframe_minutes=3,
        open_time=datetime(2026, 8, 14, hour, minute, tzinfo=timezone.utc),
        open=o, high=h, low=l, close=c,
    )


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.json")


# -- Gap 2 fix: rolling base is a genuine feedback loop -----------------------

def test_first_htf_candle_sets_no_base_only_becomes_prev():
    from strategy.state_store import StateStore as _S
    engine = RollingBaseEngine("NIFTY", _S(Path("/tmp/rb_test_1.json")))
    c1 = _htf_candle(100, 105, 98, 102)
    updated = engine.on_htf_candle_close(c1)
    assert updated is False
    assert engine.current_base is None


def test_bearish_close_through_prior_low_establishes_new_base():
    store_ = StateStore(Path("/tmp/rb_test_2.json"))
    engine = RollingBaseEngine("NIFTY", store_)
    c1 = _htf_candle(100, 105, 98, 102, minute=15)
    c2 = _htf_candle(102, 103, 90, 95, minute=30)  # closes (95) below c1.low (98) -> new base
    engine.on_htf_candle_close(c1)
    updated = engine.on_htf_candle_close(c2)

    assert updated is True
    assert engine.current_base.high == 103.0
    assert engine.current_base.low == 90.0


def test_rolling_base_updates_again_on_subsequent_break_feedback_loop():
    """This is the exact Gap 2 fix: the base keeps recalculating on every
    subsequent break, not just once."""
    store_ = StateStore(Path("/tmp/rb_test_3.json"))
    engine = RollingBaseEngine("NIFTY", store_)
    c1 = _htf_candle(100, 105, 98, 102, minute=15)
    c2 = _htf_candle(102, 103, 90, 95, minute=30)   # base #1: high=103 low=90
    c3 = _htf_candle(95, 96, 80, 82, minute=45)     # closes (82) below c2.low (90) -> base #2

    engine.on_htf_candle_close(c1)
    engine.on_htf_candle_close(c2)
    updated_again = engine.on_htf_candle_close(c3)

    assert updated_again is True
    assert engine.current_base.high == 96.0
    assert engine.current_base.low == 80.0  # base moved again, not stuck at the first one


def test_base_state_persists_across_engine_restart(tmp_path):
    path = tmp_path / "persist.json"
    store1 = StateStore(path)
    engine1 = RollingBaseEngine("NIFTY", store1)
    engine1.on_htf_candle_close(_htf_candle(100, 105, 98, 102, minute=15))
    engine1.on_htf_candle_close(_htf_candle(102, 103, 90, 95, minute=30))
    assert engine1.current_base.low == 90.0

    # Simulate a process restart: fresh StateStore + fresh engine from the same file.
    store2 = StateStore(path)
    engine2 = RollingBaseEngine("NIFTY", store2)
    assert engine2.current_base is not None
    assert engine2.current_base.low == 90.0  # survived the "restart"


# -- Gap 3 fix: void state has an explicit, coded reset condition ------------

def test_void_state_blocks_then_clears_on_retest(store):
    session_config = {"high_probability_windows": [["00:00", "23:59"]], "blocked_window": []}
    machine = InstrumentStateMachine(
        instrument_key="NIFTY", store=store, session_config=session_config,
        sweep_buffer_points=4,
    )

    machine._set_blocked(level=100.0)
    assert machine._is_blocked() is True

    # A candle whose range does NOT touch 100 -> stays blocked.
    machine._check_void_reset(_ltf_candle(110, 112, 108, 111))
    assert machine._is_blocked() is True

    # A candle whose range retests 100 -> clears.
    machine._check_void_reset(_ltf_candle(103, 104, 99, 101))
    assert machine._is_blocked() is False


def test_void_state_clears_on_new_rolling_base_even_without_retest(store):
    session_config = {"high_probability_windows": [["00:00", "23:59"]], "blocked_window": []}
    machine = InstrumentStateMachine(
        instrument_key="NIFTY", store=store, session_config=session_config,
        sweep_buffer_points=4,
    )
    machine._set_blocked(level=100.0)
    assert machine._is_blocked() is True

    # A fresh 75m base break should ALSO clear it, per Gap 3's second reset path.
    machine.on_htf_candle_close(_htf_candle(100, 105, 98, 102, minute=15))
    updated = machine.on_htf_candle_close(_htf_candle(102, 103, 90, 95, minute=30))
    assert updated is None or True  # on_htf_candle_close returns None; check state directly
    assert machine._is_blocked() is False
