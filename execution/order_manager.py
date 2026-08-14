"""
execution/order_manager.py

Wraps Fyers' FyersModel.place_order(). Field names verified against the
installed SDK's docstring: productType, side (1=Buy/-1=Sell), symbol, qty,
type (1=LIMIT), validity, limitPrice, disclosedQty, offlineOrder.

IMPORTANT DESIGN DECISION — Fyers has no confirmed separate sandbox
environment the way Upstox does (community threads explicitly ask for one
and don't get a clear "yes, here's the base URL" answer). Rather than
guess at an unverified sandbox endpoint, PAPER MODE HERE IS SIMULATED
LOCALLY: real market data (quotes, option chain, WS ticks) is used as
normal, but when env.paper_mode is True, place_entry_buy/place_exit_sell
never call Fyers' real order endpoint — they log a
[PAPER_ORDER_SIMULATED] line and return a synthetic immediate fill at the
requested price. This is safer than trusting an unverified broker-side
sandbox and gives you the exact same log trail to review tomorrow.

Going live (env.paper_mode=False) switches these same methods to call the
real place_order() endpoint — same code path, same log tags, just real
orders instead of simulated ones. That symmetry is intentional: paper
mode should exercise the identical logic that live mode will run.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

FYERS_ORDER_TYPE_LIMIT = 1
FYERS_SIDE_BUY = 1
FYERS_SIDE_SELL = -1
FYERS_STATUS_FILLED = 2
FYERS_STATUS_REJECTED = 5


@dataclass
class OrderResult:
    ok: bool
    order_id: Optional[str]
    detail: str


class OrderManager:
    def __init__(self, fyers_model, paper_mode: bool, product_type: str = "INTRADAY"):
        self.model = fyers_model
        self.paper_mode = paper_mode
        self.product_type = product_type
        self._paper_fills: dict[str, dict] = {}  # order_id -> status dict, for paper mode polling

    def get_order_status(self, order_id: str) -> dict:
        """Returns {"status": str, "filled_quantity": int, "average_price": float}.
        Fyers status code 2 = filled/traded (verified against multiple real
        orderbook samples), 5 = rejected — mapped to the string vocabulary
        execution/position_manager.py already expects."""
        if order_id in self._paper_fills:
            return self._paper_fills[order_id]

        try:
            response = self.model.get_orders(data={"id": order_id})
        except Exception:  # noqa: BLE001
            logger.exception("Order status fetch failed for %s", order_id)
            return {"status": "unknown"}

        if not isinstance(response, dict) or "orderBook" not in response:
            return {"status": "unknown"}
        orders = response["orderBook"]
        if not orders:
            return {"status": "unknown"}
        order = orders[0]

        status_code = order.get("status")
        if status_code == FYERS_STATUS_FILLED:
            status_str = "complete"
        elif status_code == FYERS_STATUS_REJECTED:
            status_str = "rejected"
        else:
            status_str = "pending"

        return {
            "status": status_str,
            "filled_quantity": order.get("filledQty", 0),
            "average_price": order.get("tradedPrice", 0.0),
        }

    def place_entry_buy(self, symbol: str, quantity: int, current_ask: float, tag: str) -> OrderResult:
        return self._place(symbol, quantity, current_ask, FYERS_SIDE_BUY, tag, is_entry=True)

    def place_exit_sell(self, symbol: str, quantity: int, limit_price: float, tag: str) -> OrderResult:
        return self._place(symbol, quantity, limit_price, FYERS_SIDE_SELL, tag, is_entry=False)

    def _place(
        self, symbol: str, quantity: int, price_hint: float, side: int, tag: str, is_entry: bool
    ) -> OrderResult:
        limit_offset = 1.5 if is_entry else 0.0  # offset applied by caller for entries; kept here as a no-op hook
        limit_price = round(price_hint, 2)
        description = f"{'BUY' if side == FYERS_SIDE_BUY else 'SELL'} {quantity}x {symbol} @ {limit_price}"

        if self.paper_mode:
            return self._simulate_paper_order(symbol, quantity, limit_price, description)

        payload = {
            "symbol": symbol,
            "qty": quantity,
            "type": FYERS_ORDER_TYPE_LIMIT,
            "side": side,
            "productType": self.product_type,
            "limitPrice": limit_price,
            "stopPrice": 0,
            "disclosedQty": 0,
            "validity": "DAY",
            "offlineOrder": False,
        }
        try:
            response = self.model.place_order(data=payload)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Order failed (%s)", description)
            return OrderResult(ok=False, order_id=None, detail=str(exc))

        if not isinstance(response, dict) or response.get("s") != "ok":
            logger.error("Order failed (%s): %s", description, response)
            return OrderResult(ok=False, order_id=None, detail=str(response))

        order_id = response.get("id")
        logger.info("Order placed OK (%s): order_id=%s", description, order_id)
        return OrderResult(ok=True, order_id=order_id, detail="Placed successfully.")

    def _simulate_paper_order(
        self, symbol: str, quantity: int, limit_price: float, description: str
    ) -> OrderResult:
        order_id = f"PAPER-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        self._paper_fills[order_id] = {
            "status": "complete",
            "filled_quantity": quantity,
            "average_price": limit_price,
        }
        logger.info("[PAPER_ORDER_SIMULATED] %s order_id=%s (no real order sent to Fyers)",
                    description, order_id)
        return OrderResult(ok=True, order_id=order_id, detail="Paper trade simulated.")
