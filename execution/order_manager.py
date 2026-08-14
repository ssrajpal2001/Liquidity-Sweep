"""
execution/order_manager.py

Wraps Upstox's OrderApiV3.place_order. Verified against the installed SDK:
    PlaceOrderV3Request(quantity, product, validity, price, tag, slice,
                         instrument_token, order_type, transaction_type,
                         disclosed_quantity, trigger_price, is_amo,
                         market_protection)

Edge Case 5 from the review: entries are always LIMIT, priced a small
offset above the live ask (for buying), never MARKET — this caps
worst-case slippage during the IV spikes sweeps often produce.

IMPORTANT — this module places REAL orders when called against a live
(non-sandbox) Configuration. Nothing in this file enforces sandbox-only;
that gate belongs in main.py / risk_controls/daily_guard.py, which should
refuse to construct an OrderManager pointed at a live ApiClient until
Phase 10's paper-trading checklist has actually been run.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import upstox_client
from upstox_client.rest import ApiException

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    ok: bool
    order_id: Optional[str]
    detail: str


class OrderManager:
    def __init__(self, api_client: "upstox_client.ApiClient", limit_offset_rupees: float):
        self.api = upstox_client.OrderApiV3(api_client)
        self._status_api = upstox_client.OrderApi(api_client)  # get_order_status lives here, not V3
        self.limit_offset_rupees = limit_offset_rupees

    def get_order_status(self, order_id: str) -> dict:
        """Returns {"status": str, "filled_quantity": int, "average_price": float}
        — field names verified against the installed SDK's OrderData
        (status, filled_quantity, average_price)."""
        try:
            response = self._status_api.get_order_status(order_id=order_id)
        except ApiException as exc:
            logger.error("Order status fetch failed for %s: %s", order_id, exc)
            return {"status": "unknown"}
        data = getattr(response, "data", None)
        if data is None:
            return {"status": "unknown"}
        return {
            "status": getattr(data, "status", "unknown"),
            "filled_quantity": getattr(data, "filled_quantity", 0),
            "average_price": getattr(data, "average_price", 0.0),
        }


    def place_entry_buy(
        self, instrument_key: str, quantity: int, current_ask: float, tag: str
    ) -> OrderResult:
        """Buys `quantity` of `instrument_key` (a CE/PE contract) as a LIMIT
        order priced current_ask + offset — near-guaranteed fill while
        capping slippage vs. a raw MARKET order."""
        limit_price = round(current_ask + self.limit_offset_rupees, 2)
        body = upstox_client.PlaceOrderV3Request(
            quantity=quantity,
            product="D",             # delivery/intraday margin product — confirm against your account type
            validity="DAY",
            price=limit_price,
            instrument_token=instrument_key,
            order_type="LIMIT",
            transaction_type="BUY",
            disclosed_quantity=0,
            trigger_price=0,
            is_amo=False,
            tag=tag,
        )
        return self._send(body, f"entry BUY {quantity}x {instrument_key} @ {limit_price}")

    def place_exit_sell(
        self, instrument_key: str, quantity: int, limit_price: float, tag: str
    ) -> OrderResult:
        body = upstox_client.PlaceOrderV3Request(
            quantity=quantity,
            product="D",
            validity="DAY",
            price=round(limit_price, 2),
            instrument_token=instrument_key,
            order_type="LIMIT",
            transaction_type="SELL",
            disclosed_quantity=0,
            trigger_price=0,
            is_amo=False,
            tag=tag,
        )
        return self._send(body, f"exit SELL {quantity}x {instrument_key} @ {limit_price}")

    def _send(self, body: "upstox_client.PlaceOrderV3Request", description: str) -> OrderResult:
        try:
            response = self.api.place_order(body)
        except ApiException as exc:
            logger.error("Order failed (%s): %s", description, exc)
            return OrderResult(ok=False, order_id=None, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error placing order (%s)", description)
            return OrderResult(ok=False, order_id=None, detail=str(exc))

        data = getattr(response, "data", None)
        order_ids = getattr(data, "order_ids", None) if data else None
        order_id = order_ids[0] if order_ids else None
        logger.info("Order placed OK (%s): order_id=%s", description, order_id)
        return OrderResult(ok=True, order_id=order_id, detail="Placed successfully.")
