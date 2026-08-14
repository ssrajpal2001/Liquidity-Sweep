from __future__ import annotations

from unittest.mock import MagicMock

from execution.order_manager import OrderResult
from execution.position_manager import Position, PositionManager, PositionStatus
from execution.risk_engine import compute_risk_plan


def _make_manager(fill_immediately=True):
    order_manager = MagicMock()
    order_manager.place_entry_buy.return_value = OrderResult(ok=True, order_id="ORD1", detail="ok")
    order_manager.place_exit_sell.return_value = OrderResult(ok=True, order_id="ORD2", detail="ok")

    def get_status(order_id):
        return {"status": "complete", "filled_quantity": 75, "average_price": 150.0}

    closed_positions = []
    manager = PositionManager(
        order_manager=order_manager,
        get_order_status_fn=get_status,
        trail_distance_points=5.0,
        target1_booking_pct=60,
        on_closed=closed_positions.append,
    )
    return manager, order_manager, closed_positions


def test_full_lifecycle_target1_then_trailing_stop_exit():
    """Simulates exactly the stages the user asked to verify tomorrow:
    entry -> fill -> target1 -> breakeven -> trailing stop -> exit."""
    manager, order_manager, closed = _make_manager()

    plan = compute_risk_plan(
        entry_premium=150.0, spot_entry=25000.0, spot_structural_sl=25020.0,
        delta=0.50, lot_size=75, total_capital_inr=200000, risk_per_trade_pct=1.0,
        target1_rr=1.5, direction="bearish",
    )
    position = manager.open_position("NSE_FO|TEST_PE", "bearish", plan, ask_price=150.0, tag="test")
    assert position is not None
    assert position.status == PositionStatus.OPEN
    assert position.filled_quantity == 75

    # Price ticks up toward Target 1 (165.0 per the risk plan)...
    manager.on_price_update(position, 160.0)
    assert position.status == PositionStatus.OPEN  # not yet at target

    manager.on_price_update(position, 165.0)  # Target 1 hit
    assert position.status == PositionStatus.TARGET1_BOOKED
    assert position.current_sl == 150.0  # moved to breakeven
    assert position.remaining_quantity == 30  # 75 - 60% = 30 remaining (round(75*0.6)=45 booked)

    # Price trails up further -> TSL should ratchet up behind it.
    manager.on_price_update(position, 180.0)
    assert position.current_sl == 175.0  # 180 - trail_distance(5)

    manager.on_price_update(position, 190.0)
    assert position.current_sl == 185.0  # ratcheted further, never down

    # Price pulls back and hits the trailing stop.
    manager.on_price_update(position, 184.0)
    assert position.status == PositionStatus.CLOSED
    assert position.exit_reason == "TSL_HIT"
    assert len(closed) == 1
    assert closed[0] is position


def test_full_lifecycle_direct_sl_hit_without_target1():
    manager, order_manager, closed = _make_manager()
    plan = compute_risk_plan(
        entry_premium=150.0, spot_entry=25000.0, spot_structural_sl=25020.0,
        delta=0.50, lot_size=75, total_capital_inr=200000, risk_per_trade_pct=1.0,
        target1_rr=1.5, direction="bearish",
    )
    position = manager.open_position("NSE_FO|TEST_PE", "bearish", plan, ask_price=150.0, tag="test")
    assert position.current_sl == plan.premium_sl_price  # 140.0

    manager.on_price_update(position, 145.0)  # still fine
    assert position.status == PositionStatus.OPEN

    manager.on_price_update(position, 139.0)  # hits SL before ever reaching Target 1
    assert position.status == PositionStatus.CLOSED
    assert position.exit_reason == "SL_HIT"
    assert len(closed) == 1


def test_entry_not_filled_returns_none_and_does_not_crash():
    order_manager = MagicMock()
    order_manager.place_entry_buy.return_value = OrderResult(ok=True, order_id="ORD1", detail="ok")

    def get_status_never_fills(order_id):
        return {"status": "open", "filled_quantity": 0, "average_price": 0.0}

    manager = PositionManager(
        order_manager=order_manager, get_order_status_fn=get_status_never_fills,
        trail_distance_points=5.0, target1_booking_pct=60,
    )
    plan = compute_risk_plan(
        entry_premium=150.0, spot_entry=25000.0, spot_structural_sl=25020.0,
        delta=0.50, lot_size=75, total_capital_inr=200000, risk_per_trade_pct=1.0,
        target1_rr=1.5, direction="bearish",
    )
    # Force fast timeout for the test rather than waiting the full 30s.
    import execution.position_manager as pm
    pm.ORDER_FILL_POLL_TIMEOUT_SECONDS = 0.1
    pm.ORDER_FILL_POLL_INTERVAL_SECONDS = 0.05

    position = manager.open_position("NSE_FO|TEST_PE", "bearish", plan, ask_price=150.0, tag="test")
    assert position is None  # never filled -> no position, no crash
