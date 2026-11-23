"""LangGraph durability mode scenarios (claim LG-C06 / LG-C05 / LG-C08).

Two-node order workflow (`charge -> confirm`) backed by `SqliteSaver`:
1. `durability_async`: default `durability="async"`, no idempotency key (how LangGraph's
   quickstarts write graphs).
2. `durability_sync_no_key`: `durability="sync"`, no idempotency key.
3. `durability_sync_fixed`: `durability="sync"` + idempotency keys derived from durable
   graph state (`f"{thread_id}:charge"`, `f"{thread_id}:confirm"`).

Run with:
    shape test examples/langgraph/durability.py --seeds 200 --jobs 4
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from payments import OrderProvider, PaymentProvider

import shape

THREAD_ID = "order-42"
ORDER_ID = "ord-42"
AMOUNT = 5000


class OrderState(TypedDict):
    thread_id: str
    order_id: str
    amount: int
    charge_id: int | None
    confirm_id: int | None


class CheckoutClient:
    def __init__(self, db_path: Path) -> None:
        self.payments = PaymentProvider(db_path)
        self.orders = OrderProvider(db_path)

    @shape.effect(
        "payments.charge",
        scope=lambda order_id, **_: f"order:{order_id}",
        key=lambda idempotency_key=None, **_: idempotency_key,
    )
    def charge(self, order_id: str, amount: int, idempotency_key: str | None = None) -> int:
        return self.payments.charge(order_id, amount, idempotency_key)

    @shape.effect(
        "orders.confirm",
        scope=lambda order_id, **_: f"order:{order_id}",
        key=lambda idempotency_key=None, **_: idempotency_key,
    )
    def confirm(
        self,
        order_id: str,
        charge_id: int,
        idempotency_key: str | None = None,
    ) -> int:
        return self.orders.confirm(order_id, charge_id, idempotency_key)

    def close(self) -> None:
        self.payments.close()
        self.orders.close()


def _run_order_graph(
    run: shape.Run,
    *,
    durability: str | None,
    use_idempotency_key: bool,
) -> None:
    client = CheckoutClient(run.dir / "provider.db")
    try:
        with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as checkpointer:
            def charge_node(state: OrderState) -> dict[str, int]:
                key = f"{state['thread_id']}:charge" if use_idempotency_key else None
                charge_id = client.charge(
                    order_id=state["order_id"],
                    amount=state["amount"],
                    idempotency_key=key,
                )
                return {"charge_id": charge_id}

            def confirm_node(state: OrderState) -> dict[str, int]:
                key = f"{state['thread_id']}:confirm" if use_idempotency_key else None
                confirm_id = client.confirm(
                    order_id=state["order_id"],
                    charge_id=state["charge_id"],
                    idempotency_key=key,
                )
                return {"confirm_id": confirm_id}

            builder = StateGraph(OrderState)
            builder.add_node("charge", charge_node)
            builder.add_node("confirm", confirm_node)
            builder.add_edge(START, "charge")
            builder.add_edge("charge", "confirm")
            builder.add_edge("confirm", END)
            graph = builder.compile(checkpointer=checkpointer)

            config = {"configurable": {"thread_id": THREAD_ID}}
            kwargs = {"durability": durability} if durability is not None else {}
            if run.restarts == 0:
                graph.invoke(
                    {
                        "thread_id": THREAD_ID,
                        "order_id": ORDER_ID,
                        "amount": AMOUNT,
                        "charge_id": None,
                        "confirm_id": None,
                    },
                    config,
                    **kwargs,
                )
            else:
                graph.invoke(None, config, **kwargs)
    finally:
        client.close()


CHARGE_ONCE = [
    shape.at_most_once("payments.charge"),
    shape.exactly_once("payments.charge"),
    shape.at_most_once("orders.confirm"),
    shape.exactly_once("orders.confirm"),
]


@shape.scenario(invariants=CHARGE_ONCE, max_restarts=3)
def durability_async(run: shape.Run) -> None:
    """Default durability='async', no idempotency key."""
    _run_order_graph(run, durability=None, use_idempotency_key=False)


@shape.scenario(invariants=CHARGE_ONCE, max_restarts=3)
def durability_async_with_key(run: shape.Run) -> None:
    """Default durability='async' + idempotency keys (tests whether async still loses steps)."""
    _run_order_graph(run, durability=None, use_idempotency_key=True)


@shape.scenario(invariants=CHARGE_ONCE, max_restarts=3)
def durability_sync_no_key(run: shape.Run) -> None:
    """durability='sync', no idempotency key (mid-node crash_after still repeats the effect)."""
    _run_order_graph(run, durability="sync", use_idempotency_key=False)


@shape.scenario(invariants=CHARGE_ONCE, max_restarts=3)
def durability_sync_fixed(run: shape.Run) -> None:
    """durability='sync' + deterministic idempotency keys derived from durable state."""
    _run_order_graph(run, durability="sync", use_idempotency_key=True)
