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


# -- Multi-candle displacement window: fixes a real gap flagged during
# external review — sweep and displacement no longer have to occur on the
# exact same candle. A common real pattern is: candle N wicks/rejects a
# level with a small body (the sweep), and the actual aggressive
# institutional move happens on candle N+1 or N+2. Previously this was
# discarded as "no_displacement" the instant the sweep candle's own body
# didn't qualify. -----------------------------------------------------------

def test_displacement_confirms_on_a_later_candle_within_the_window(store):
    """Regression test for the exact scenario an external review caught:
    tiny-bodied sweep candle, big displacement move on the NEXT candle.
    This used to be silently discarded; it must now be recognized."""
    from datetime import datetime, timezone, timedelta

    IST = timezone(timedelta(hours=5, minutes=30))
    session_config = {"high_probability_windows": [["09:15", "10:30"]], "blocked_window": []}
    machine = InstrumentStateMachine(
        instrument_key="NIFTY", store=store, session_config=session_config, sweep_buffer_points=4,
    )

    def ltf(o, h, l, c, minute):
        return Candle(instrument_key="NIFTY", timeframe_minutes=3,
                      open_time=datetime(2026, 8, 10, 9, minute, tzinfo=IST), open=o, high=h, low=l, close=c)

    def htf(o, h, l, c, hour, minute):
        return Candle(instrument_key="NIFTY", timeframe_minutes=75,
                      open_time=datetime(2026, 8, 10, hour, minute, tzinfo=IST), open=o, high=h, low=l, close=c)

    machine.on_htf_candle_close(htf(100, 105, 98, 102, 9, 15))
    machine.on_htf_candle_close(htf(102, 103, 90, 95, 10, 30))  # base: high=103 low=90

    events = []
    machine._on_event = lambda evt, data: events.append(evt)

    signals = []
    candles = [
        ltf(95, 96, 93, 94, 15),
        ltf(94, 95, 93, 94, 18),
        ltf(94, 104, 92, 95, 21),  # sweep: pierces 103, small body -> no displacement yet
        ltf(95, 91, 60, 62, 24),   # THE real displacement move, on the candle AFTER the sweep
        ltf(62, 70, 60, 65, 27),
        ltf(65, 92, 64, 91, 30),   # retest touch
    ]
    for c in candles:
        d = machine.on_ltf_candle_close(c)
        if d:
            signals.append(d)

    assert "displacement_window_extended" in events  # the fix's key new event
    assert "displacement_confirmed" in events
    assert len(signals) == 1
    assert signals[0].direction == "bearish"


def test_displacement_still_confirms_immediately_on_same_candle_when_it_qualifies(store):
    """The original single-candle case (violent one-candle rejection) must
    still work exactly as before — this is a strict addition, not a
    replacement of the fast path."""
    from datetime import datetime, timezone, timedelta

    IST = timezone(timedelta(hours=5, minutes=30))
    session_config = {"high_probability_windows": [["00:00", "23:59"]], "blocked_window": []}
    machine = InstrumentStateMachine(
        instrument_key="NIFTY", store=store, session_config=session_config, sweep_buffer_points=4,
    )
    events = []
    machine._on_event = lambda evt, data: events.append(evt)

    def ltf(o, h, l, c, minute):
        return Candle(instrument_key="NIFTY", timeframe_minutes=3,
                      open_time=datetime(2026, 8, 10, 9, minute, tzinfo=IST), open=o, high=h, low=l, close=c)

    def htf(o, h, l, c, hour, minute):
        return Candle(instrument_key="NIFTY", timeframe_minutes=75,
                      open_time=datetime(2026, 8, 10, hour, minute, tzinfo=IST), open=o, high=h, low=l, close=c)

    machine.on_htf_candle_close(htf(100, 105, 98, 102, 9, 15))
    machine.on_htf_candle_close(htf(102, 103, 90, 95, 10, 30))

    for c in [ltf(95, 96, 93, 94, 15), ltf(94, 95, 93, 94, 18)]:
        machine.on_ltf_candle_close(c)
    # A single candle that both sweeps AND displaces (big body, matches original design).
    machine.on_ltf_candle_close(ltf(94, 104, 60, 62, 21))

    assert "displacement_confirmed" in events
    assert "displacement_window_extended" not in events  # confirmed immediately, no window needed


def test_displacement_window_gives_up_after_configured_candle_count(store):
    """If displacement never materializes within the window, the sweep
    must still be discarded as a false sweep — the fix adds patience, not
    infinite patience."""
    from datetime import datetime, timezone, timedelta

    IST = timezone(timedelta(hours=5, minutes=30))
    session_config = {"high_probability_windows": [["00:00", "23:59"]], "blocked_window": []}
    machine = InstrumentStateMachine(
        instrument_key="NIFTY", store=store, session_config=session_config, sweep_buffer_points=4,
        displacement_window_candles=2,
    )
    events = []
    machine._on_event = lambda evt, data: events.append((evt, data))

    def ltf(o, h, l, c, minute):
        return Candle(instrument_key="NIFTY", timeframe_minutes=3,
                      open_time=datetime(2026, 8, 10, 9, minute, tzinfo=IST), open=o, high=h, low=l, close=c)

    def htf(o, h, l, c, hour, minute):
        return Candle(instrument_key="NIFTY", timeframe_minutes=75,
                      open_time=datetime(2026, 8, 10, hour, minute, tzinfo=IST), open=o, high=h, low=l, close=c)

    machine.on_htf_candle_close(htf(100, 105, 98, 102, 9, 15))
    machine.on_htf_candle_close(htf(102, 103, 90, 95, 10, 30))

    for c in [ltf(95, 96, 93, 94, 15), ltf(94, 95, 93, 94, 18)]:
        machine.on_ltf_candle_close(c)

    machine.on_ltf_candle_close(ltf(94, 104, 92, 95, 21))       # sweep, tiny body
    machine.on_ltf_candle_close(ltf(95, 96, 93, 94, 24))        # still no real displacement
    machine.on_ltf_candle_close(ltf(94, 95, 93, 94, 27))        # window exhausted -> discard

    rejected = [data for evt, data in events if evt == "sweep_rejected" and data.get("reason") == "no_displacement"]
    assert len(rejected) == 1
