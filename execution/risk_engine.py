"""
execution/risk_engine.py

Pure math, no I/O — fully unit-testable. Implements exactly the formulas
from the approved architecture:

    Option SL (points)   = Spot Risk (points) x Delta
    Lots = Capital-at-Risk (INR) / (Spot Risk Points x Delta x Lot Size)
    Target 1 (R:R from config.execution.target1_rr) -> book target1_booking_pct%
    Target 2 = opposing PDH/PDL or unmitigated HTF zone (passed in by caller,
               since it depends on structure the state machine already knows)
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class RiskPlan:
    spot_risk_points: float
    premium_sl_points: float
    premium_sl_price: float
    entry_price: float
    lots: int
    quantity: int
    capital_at_risk_inr: float
    target1_price: float
    target1_rr: float


def compute_risk_plan(
    entry_premium: float,
    spot_entry: float,
    spot_structural_sl: float,
    delta: float,
    lot_size: int,
    total_capital_inr: float,
    risk_per_trade_pct: float,
    target1_rr: float,
    direction: str,  # "bearish" (PUT, spot_sl above entry) or "bullish" (CALL, spot_sl below entry)
) -> RiskPlan:
    spot_risk_points = abs(spot_entry - spot_structural_sl)
    abs_delta = abs(delta)

    premium_sl_points = spot_risk_points * abs_delta
    premium_sl_price = max(0.05, entry_premium - premium_sl_points)  # long option, SL below entry premium

    capital_at_risk = total_capital_inr * (risk_per_trade_pct / 100)
    risk_per_lot = premium_sl_points * lot_size
    lots = max(1, math.floor(capital_at_risk / risk_per_lot)) if risk_per_lot > 0 else 1
    quantity = lots * lot_size

    target1_price = entry_premium + (premium_sl_points * target1_rr)

    return RiskPlan(
        spot_risk_points=spot_risk_points,
        premium_sl_points=premium_sl_points,
        premium_sl_price=round(premium_sl_price, 2),
        entry_price=entry_premium,
        lots=lots,
        quantity=quantity,
        capital_at_risk_inr=round(capital_at_risk, 2),
        target1_price=round(target1_price, 2),
        target1_rr=target1_rr,
    )
