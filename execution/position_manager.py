"""
execution/position_manager.py

Owns one open position end-to-end and logs every stage with a distinct,
greppable tag — this is deliberately verbose because tomorrow's whole
verification plan is reading logs/bot.log to confirm each stage actually
fired. Every log line below starts with a tag in [BRACKETS] so you (or I,
reading a pasted log) can `grep` for e.g. "[TSL_UPDATE]" and see exactly
when and why the stop moved.

Stages, in order:
  [ENTRY_ORDER_PLACED]  -> [ENTRY_FILLED] (polls order status)
  [TARGET1_HIT]         -> books target1_booking_pct%, [SL_MOVED_TO_BREAKEVEN]
  [TSL_UPDATE]          -> trailing stop ratchets up (never down) as price
                            moves further favorably past Target 1
  [TARGET2_HIT] or [SL_HIT] or [TSL_HIT] -> [POSITION_CLOSED]

Trailing stop rule (this is the "TSL" you asked about — not in the
original architecture doc as a separate concept, folded in here as: once
Target 1 is booked and SL is at breakeven, the remaining runner's SL
trails behind the best price seen by `trail_distance_points`, ratcheting
only in the profitable direction, same as the fixed SL/Target math already
verified in execution/risk_engine.py).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from execution.order_manager import OrderManager, OrderResult
from execution.risk_engine import RiskPlan

logger = logging.getLogger(__name__)

ORDER_FILL_POLL_INTERVAL_SECONDS = 2
ORDER_FILL_POLL_TIMEOUT_SECONDS = 30


class PositionStatus(str, Enum):
    PENDING_ENTRY = "pending_entry"
    OPEN = "open"
    TARGET1_BOOKED = "target1_booked"
    CLOSED = "closed"


@dataclass
class Position:
    instrument_key: str
    direction: str  # "bearish" (PE) or "bullish" (CE)
    plan: RiskPlan
    tag: str

    status: PositionStatus = PositionStatus.PENDING_ENTRY
    entry_order_id: Optional[str] = None
    filled_quantity: int = 0
    remaining_quantity: int = 0
    current_sl: float = 0.0
    best_price_seen: float = 0.0
    realized_pnl_inr: float = 0.0
    exit_reason: Optional[str] = None


class PositionManager:
    def __init__(
        self,
        order_manager: OrderManager,
        get_order_status_fn,   # Callable[[str], dict] -> {"status": str, "filled_quantity": int, "average_price": float}
        trail_distance_points: float,
        target1_booking_pct: int,
        on_closed=None,        # Callable[[Position], None] — e.g. daily_guard.record_trade_closed
    ):
        self.order_manager = order_manager
        self.get_order_status_fn = get_order_status_fn
        self.trail_distance_points = trail_distance_points
        self.target1_booking_pct = target1_booking_pct
        self.on_closed = on_closed

    def open_position(self, instrument_key: str, direction: str, plan: RiskPlan, ask_price: float, tag: str) -> Optional[Position]:
        result = self.order_manager.place_entry_buy(instrument_key, plan.quantity, ask_price, tag)
        logger.info(
            "[ENTRY_ORDER_PLACED] %s qty=%d limit=%.2f order_id=%s ok=%s",
            instrument_key, plan.quantity, ask_price + 0, result.order_id, result.ok,
        )
        if not result.ok:
            logger.error("[ENTRY_ORDER_FAILED] %s detail=%s", instrument_key, result.detail)
            return None

        position = Position(
            instrument_key=instrument_key, direction=direction, plan=plan, tag=tag,
            entry_order_id=result.order_id, current_sl=plan.premium_sl_price,
            best_price_seen=plan.entry_price,
        )
        filled = self._wait_for_fill(position)
        if not filled:
            logger.error(
                "[ENTRY_NOT_FILLED] %s order_id=%s did not fill within %ds — treating as no position.",
                instrument_key, result.order_id, ORDER_FILL_POLL_TIMEOUT_SECONDS,
            )
            return None
        return position

    def _wait_for_fill(self, position: Position) -> bool:
        deadline = time.time() + ORDER_FILL_POLL_TIMEOUT_SECONDS
        while time.time() < deadline:
            status = self.get_order_status_fn(position.entry_order_id)
            if status.get("status", "").lower() in ("complete", "filled"):
                position.filled_quantity = status.get("filled_quantity", position.plan.quantity)
                position.remaining_quantity = position.filled_quantity
                position.status = PositionStatus.OPEN
                logger.info(
                    "[ENTRY_FILLED] %s qty=%d avg_price=%.2f",
                    position.instrument_key, position.filled_quantity,
                    status.get("average_price", position.plan.entry_price),
                )
                return True
            time.sleep(ORDER_FILL_POLL_INTERVAL_SECONDS)
        return False

    def on_price_update(self, position: Position, current_premium: float) -> None:
        """Call on every LTF candle close (or tick) for instruments with an
        open position. Drives SL / TSL / Target checks."""
        if position.status == PositionStatus.CLOSED:
            return

        position.best_price_seen = max(position.best_price_seen, current_premium)

        if position.status == PositionStatus.OPEN and current_premium >= position.plan.target1_price:
            self._book_target1(position, current_premium)
            return

        if position.status == PositionStatus.TARGET1_BOOKED:
            new_trail_sl = position.best_price_seen - self.trail_distance_points
            if new_trail_sl > position.current_sl:
                old_sl = position.current_sl
                position.current_sl = new_trail_sl
                logger.info(
                    "[TSL_UPDATE] %s sl %.2f -> %.2f (best_price=%.2f, trail_distance=%.2f)",
                    position.instrument_key, old_sl, new_trail_sl,
                    position.best_price_seen, self.trail_distance_points,
                )

        if current_premium <= position.current_sl:
            reason = "TSL_HIT" if position.status == PositionStatus.TARGET1_BOOKED else "SL_HIT"
            self._close_position(position, current_premium, reason)

    def _book_target1(self, position: Position, current_premium: float) -> None:
        book_qty = round(position.remaining_quantity * (self.target1_booking_pct / 100))
        book_qty = max(1, min(book_qty, position.remaining_quantity))

        result = self.order_manager.place_exit_sell(
            position.instrument_key, book_qty, current_premium, tag=f"{position.tag}-t1"
        )
        logger.info(
            "[TARGET1_HIT] %s booking qty=%d at %.2f ok=%s order_id=%s",
            position.instrument_key, book_qty, current_premium, result.ok, result.order_id,
        )
        if result.ok:
            position.remaining_quantity -= book_qty
            position.realized_pnl_inr += book_qty * (current_premium - position.plan.entry_price)
            position.status = PositionStatus.TARGET1_BOOKED
            old_sl = position.current_sl
            position.current_sl = position.plan.entry_price  # move to breakeven
            logger.info(
                "[SL_MOVED_TO_BREAKEVEN] %s sl %.2f -> %.2f",
                position.instrument_key, old_sl, position.current_sl,
            )
            if position.remaining_quantity <= 0:
                self._close_position(position, current_premium, "TARGET1_FULL_EXIT")

    def _close_position(self, position: Position, exit_premium: float, reason: str) -> None:
        if position.remaining_quantity > 0:
            result = self.order_manager.place_exit_sell(
                position.instrument_key, position.remaining_quantity, exit_premium,
                tag=f"{position.tag}-exit",
            )
            logger.info(
                "[%s] %s exiting remaining qty=%d at %.2f ok=%s order_id=%s",
                reason, position.instrument_key, position.remaining_quantity,
                exit_premium, result.ok, result.order_id,
            )
            if result.ok:
                position.realized_pnl_inr += position.remaining_quantity * (
                    exit_premium - position.plan.entry_price
                )
                position.remaining_quantity = 0

        position.status = PositionStatus.CLOSED
        position.exit_reason = reason
        logger.info(
            "[POSITION_CLOSED] %s reason=%s realized_pnl_inr=%.2f",
            position.instrument_key, reason, position.realized_pnl_inr,
        )
        if self.on_closed is not None:
            self.on_closed(position)
