"""Workload 1: charge-once.

A deterministic LangGraph workflow that charges an order and then confirms it.
All side effects go to the external god's-eye WorldLog. No LLM is used.

Variants:
  - naive:           Idiomatic 2-node StateGraph (charge -> confirm), default
                     durability ("async"), non-idempotent external calls.
  - sync:            Same 2-node StateGraph with durability="sync".
  - task_async:      Single node wrapping charge and confirm in @task (per
                     graph-api.md#using-tasks-in-nodes), default durability.
  - idempotent_sync: Docs-recommended mitigation: 2-node StateGraph,
                     durability="sync", and deterministic idempotency keys
                     derived from durable state (fault-tolerance.md).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import EmptyInputError
from langgraph.func import task
from langgraph.graph import END, START, StateGraph

from world import WorldLog


class OrderState(TypedDict):
    order_id: str
    amount_cents: int
    charge_receipt: str | None
    confirm_receipt: str | None


def maybe_crash(point: str) -> None:
    if os.environ.get("CRASH_POINT") == point:
        os.kill(os.getpid(), signal.SIGKILL)


def build_two_node_graph(
    checkpointer: SqliteSaver,
    world: WorldLog,
    *,
    use_idempotency_key: bool,
):
    def charge_node(state: OrderState) -> dict[str, object]:
        key = f"charge:{state['order_id']}" if use_idempotency_key else None
        receipt = world.execute_effect(
            op="charge",
            order_id=state["order_id"],
            amount_cents=state["amount_cents"],
            idempotency_key=key,
        )
        maybe_crash("after_charge_before_checkpoint")
        return {"charge_receipt": receipt["receipt_id"]}

    def confirm_node(state: OrderState) -> dict[str, object]:
        maybe_crash("after_charge_returns")
        key = f"confirm:{state['order_id']}" if use_idempotency_key else None
        receipt = world.execute_effect(
            op="confirm",
            order_id=state["order_id"],
            amount_cents=0,
            idempotency_key=key,
        )
        maybe_crash("mid_confirm_after_effect")
        return {"confirm_receipt": receipt["receipt_id"]}

    builder = StateGraph(OrderState)
    builder.add_node("charge", charge_node)
    builder.add_node("confirm", confirm_node)
    builder.add_edge(START, "charge")
    builder.add_edge("charge", "confirm")
    builder.add_edge("confirm", END)
    return builder.compile(checkpointer=checkpointer)


def build_task_graph(checkpointer: SqliteSaver, world: WorldLog):
    @task
    def charge_task(order_id: str, amount_cents: int) -> str:
        receipt = world.execute_effect(
            op="charge",
            order_id=order_id,
            amount_cents=amount_cents,
            idempotency_key=None,
        )
        maybe_crash("after_charge_before_checkpoint")
        return str(receipt["receipt_id"])

    @task
    def confirm_task(order_id: str, charge_receipt: str) -> str:
        receipt = world.execute_effect(
            op="confirm",
            order_id=order_id,
            amount_cents=0,
            idempotency_key=None,
        )
        maybe_crash("mid_confirm_after_effect")
        return str(receipt["receipt_id"])

    def process_order(state: OrderState) -> dict[str, object]:
        charge_receipt = charge_task(state["order_id"], state["amount_cents"]).result()
        maybe_crash("after_charge_returns")
        confirm_receipt = confirm_task(state["order_id"], charge_receipt).result()
        return {
            "charge_receipt": charge_receipt,
            "confirm_receipt": confirm_receipt,
        }

    builder = StateGraph(OrderState)
    builder.add_node("process_order", process_order)
    builder.add_edge(START, "process_order")
    builder.add_edge("process_order", END)
    return builder.compile(checkpointer=checkpointer)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--variant",
        required=True,
        choices=["naive", "sync", "task_async", "idempotent_sync"],
    )
    parser.add_argument("--phase", required=True, choices=["run", "recover"])
    parser.add_argument("--checkpoint-db", required=True)
    parser.add_argument("--world-db", required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--order-id", required=True)
    args = parser.parse_args()

    world = WorldLog(args.world_db)
    conn = sqlite3.connect(args.checkpoint_db, check_same_thread=False)
    try:
        checkpointer = SqliteSaver(conn)
        if args.variant == "task_async":
            graph = build_task_graph(checkpointer, world)
        else:
            graph = build_two_node_graph(
                checkpointer,
                world,
                use_idempotency_key=(args.variant == "idempotent_sync"),
            )

        config = {"configurable": {"thread_id": args.thread_id}}
        invoke_kwargs: dict[str, object] = {}
        if args.variant in ("sync", "idempotent_sync"):
            invoke_kwargs["durability"] = "sync"

        if args.phase == "run":
            payload: OrderState = {
                "order_id": args.order_id,
                "amount_cents": 5000,
                "charge_receipt": None,
                "confirm_receipt": None,
            }
            result = graph.invoke(payload, config, **invoke_kwargs)
            print(json.dumps({"outcome": "completed", "state": result}))
        else:
            try:
                result = graph.invoke(None, config, **invoke_kwargs)
                print(json.dumps({"outcome": "completed", "state": result}))
            except EmptyInputError as exc:
                print(json.dumps({"outcome": "empty_input_error", "error": str(exc)}))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
