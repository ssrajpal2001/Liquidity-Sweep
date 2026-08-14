"""
brokers/registry.py

Maps a broker id string (what a future UI's dropdown would send) to its
BrokerAdapter class. Adding a new broker is: write the adapter class,
add one line here. Nothing else in the app needs to change — main.py
and every strategy/execution module only ever depend on brokers.base.
BrokerAdapter, never on a concrete class name.

Multi-client note (per the plug-and-play plan): a real multi-tenant
system would key adapter INSTANCES by (client_id, broker_name) —
BROKER_REGISTRY below only maps broker NAME -> ADAPTER CLASS, which is
the type-level plug-in point. Instantiating one adapter per logged-in
client's credentials is the session-manager layer that sits on top of
this registry — not built yet, see the phased plan in chat.
"""
from __future__ import annotations

from typing import Type

from brokers.base import BrokerAdapter
from brokers.fyers_adapter import FyersBrokerAdapter

BROKER_REGISTRY: dict[str, Type[BrokerAdapter]] = {
    "fyers": FyersBrokerAdapter,
    # "upstox": UpstoxBrokerAdapter,   # re-add when brokers/upstox_adapter.py exists
}


def available_brokers() -> list[str]:
    """What a future UI's broker dropdown would list."""
    return sorted(BROKER_REGISTRY.keys())


def get_adapter_class(broker_name: str) -> Type[BrokerAdapter]:
    try:
        return BROKER_REGISTRY[broker_name]
    except KeyError:
        raise ValueError(
            f"Unknown broker '{broker_name}'. Available: {available_brokers()}"
        ) from None
