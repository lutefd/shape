"""LangGraph durability mode scenarios (claim LG-C06 / LG-C05 / LG-C08), using `shape.langgraph.drive`.

Run with:
    shape test examples/langgraph/durability.py --seeds 200 --jobs 4
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from payments import OrderProvider, PaymentProvider

import shape
import shape.langgraph

THREAD_ID = "order-42"
INIT_STATE = {
    "thread_id": THREAD_ID,
    "order_id": "ord-42",
    "amount": 5000,
    "charge_id": None,
    "confirm_id": None,
}
CONFIG = {"configurable": {"thread_id": THREAD_ID}}
CHARGE_ONCE = [
    shape.at_most_once("payments.charge"),
    shape.exactly_once("payments.charge"),
    shape.at_most_once("orders.confirm"),
    shape.exactly_once("orders.confirm"),
]


class OrderState(TypedDict):
    thread_id: str
    order_id: str
    amount: int
    charge_id: int | None
    confirm_id: int | None


def _run_order_graph(
    run: shape.Run,
    *,
    durability: str | None,
    use_idempotency_key: bool,
) -> None:
    payments = PaymentProvider(run.dir / "provider.db")
    orders = OrderProvider(run.dir / "provider.db")

    @shape.effect(
        "payments.charge",
        scope=lambda order_id, **_: f"order:{order_id}",
        key=lambda idempotency_key=None, **_: idempotency_key,
    )
    def charge(order_id: str, amount: int, idempotency_key: str | None = None) -> int:
        return payments.charge(order_id, amount, idempotency_key)

    @shape.effect(
        "orders.confirm",
        scope=lambda order_id, **_: f"order:{order_id}",
        key=lambda idempotency_key=None, **_: idempotency_key,
    )
    def confirm(order_id: str, charge_id: int, idempotency_key: str | None = None) -> int:
        return orders.confirm(order_id, charge_id, idempotency_key)

    try:
        with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as checkpointer:
            def charge_node(state: OrderState) -> dict[str, int]:
                key = f"{state['thread_id']}:charge" if use_idempotency_key else None
                return {"charge_id": charge(state["order_id"], state["amount"], key)}

            def confirm_node(state: OrderState) -> dict[str, int]:
                key = f"{state['thread_id']}:confirm" if use_idempotency_key else None
                return {"confirm_id": confirm(state["order_id"], state["charge_id"], key)}

            builder = StateGraph(OrderState)
            builder.add_node("charge", charge_node)
            builder.add_node("confirm", confirm_node)
            builder.add_edge(START, "charge")
            builder.add_edge("charge", "confirm")
            builder.add_edge("confirm", END)
            graph = builder.compile(checkpointer=checkpointer)
            shape.langgraph.drive(graph, INIT_STATE, CONFIG, run, durability=durability)
    finally:
        payments.close()
        orders.close()


@shape.scenario(invariants=CHARGE_ONCE, max_restarts=3)
def durability_async(run: shape.Run) -> None:
    """Default durability='async', no idempotency key."""
    _run_order_graph(run, durability=None, use_idempotency_key=False)


@shape.scenario(invariants=CHARGE_ONCE, max_restarts=3)
def durability_async_with_key(run: shape.Run) -> None:
    """Default durability='async' + idempotency keys."""
    _run_order_graph(run, durability=None, use_idempotency_key=True)


@shape.scenario(invariants=CHARGE_ONCE, max_restarts=3)
def durability_sync_no_key(run: shape.Run) -> None:
    """durability='sync', no idempotency key (mid-node crash_after still repeats the effect)."""
    _run_order_graph(run, durability="sync", use_idempotency_key=False)


@shape.scenario(invariants=CHARGE_ONCE, max_restarts=3)
def durability_sync_fixed(run: shape.Run) -> None:
    """durability='sync' + deterministic idempotency keys derived from durable state."""
    _run_order_graph(run, durability="sync", use_idempotency_key=True)
