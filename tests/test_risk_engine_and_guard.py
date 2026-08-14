from __future__ import annotations

from execution.risk_engine import compute_risk_plan
from risk_controls.daily_guard import DailyGuard


def test_risk_plan_matches_worked_example_from_the_strategy_notes():
    # From the original discussion: Spot Risk 20 points, Delta 0.50 -> Option SL = 10 pts.
    plan = compute_risk_plan(
        entry_premium=150.0,
        spot_entry=25000.0,
        spot_structural_sl=25020.0,  # 20 points away (bearish/PUT: SL above entry)
        delta=0.50,
        lot_size=75,
        total_capital_inr=200000,
        risk_per_trade_pct=1.0,
        target1_rr=1.5,
        direction="bearish",
    )
    assert plan.spot_risk_points == 20.0
    assert plan.premium_sl_points == 10.0
    assert plan.premium_sl_price == 140.0  # 150 - 10
    assert plan.target1_price == 165.0     # 150 + (10 * 1.5)


def test_risk_plan_position_sizing_respects_capital_at_risk():
    plan = compute_risk_plan(
        entry_premium=100.0, spot_entry=25000.0, spot_structural_sl=25020.0,
        delta=0.50, lot_size=75, total_capital_inr=200000, risk_per_trade_pct=1.0,
        target1_rr=1.5, direction="bearish",
    )
    # capital_at_risk = 2000; risk_per_lot = 10 pts * 75 = 750 -> floor(2000/750) = 2 lots
    assert plan.capital_at_risk_inr == 2000.0
    assert plan.lots == 2
    assert plan.quantity == 150


def test_risk_plan_never_returns_zero_lots():
    plan = compute_risk_plan(
        entry_premium=100.0, spot_entry=25000.0, spot_structural_sl=25200.0,  # huge SL distance
        delta=0.50, lot_size=75, total_capital_inr=50000, risk_per_trade_pct=1.0,
        target1_rr=1.5, direction="bearish",
    )
    assert plan.lots >= 1  # even if risk-based sizing would round to 0, we take at least 1 lot


# -- daily guard ---------------------------------------------------------

def test_daily_guard_allows_trading_initially():
    guard = DailyGuard(total_capital_inr=200000, max_daily_loss_pct=3.0, max_trades_per_day=3)
    can_trade, reason = guard.can_trade()
    assert can_trade is True


def test_daily_guard_trips_on_max_loss():
    guard = DailyGuard(total_capital_inr=200000, max_daily_loss_pct=3.0, max_trades_per_day=10)
    guard.record_trade_closed(-6001)  # 3% of 200000 = 6000
    can_trade, reason = guard.can_trade()
    assert can_trade is False
    assert "max loss" in reason.lower()


def test_daily_guard_trips_on_max_trades():
    guard = DailyGuard(total_capital_inr=200000, max_daily_loss_pct=50.0, max_trades_per_day=2)
    guard.record_trade_closed(100)
    guard.record_trade_closed(100)
    can_trade, reason = guard.can_trade()
    assert can_trade is False
    assert "max trades" in reason.lower()


def test_daily_guard_manual_halt_and_resume():
    guard = DailyGuard(total_capital_inr=200000, max_daily_loss_pct=3.0, max_trades_per_day=10)
    guard.halt()
    can_trade, reason = guard.can_trade()
    assert can_trade is False
    assert "halted" in reason.lower()

    guard.resume()
    can_trade, _ = guard.can_trade()
    assert can_trade is True
