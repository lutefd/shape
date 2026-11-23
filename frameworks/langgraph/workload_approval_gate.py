"""Workload 2: approval-gate.

A deterministic LangGraph human-in-the-loop workflow where an irreversible
action (wire_transfer) is gated behind `interrupt()` and resumed via
`Command(resume=True|False)`. No LLM is used.

Variants:
  - naive_two_node: Idiomatic 2-node approval workflow from
                    interrupts.md#approve-or-reject (approval -> execute|cancel),
                    default durability ("async"), non-idempotent external call.
  - sync_two_node:  Same 2-node graph with durability="sync", non-idempotent
                    external call.
  - mitigated:      Docs-recommended mitigation: separate approval and execute
                    nodes (interrupts.md), durability="sync" (checkpointers.md),
                    and deterministic idempotency key derived from durable state
                    (fault-tolerance.md).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
from typing import Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import EmptyInputError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from world import WorldLog


class ApprovalState(TypedDict):
    order_id: str
    amount_cents: int
    status: Literal["pending", "executed", "rejected"]
    tx_receipt: str | None


def maybe_crash(point: str) -> None:
    if os.environ.get("CRASH_POINT") == point:
        os.kill(os.getpid(), signal.SIGKILL)


def build_approval_graph(
    checkpointer: SqliteSaver,
    world: WorldLog,
    *,
    use_idempotency_key: bool,
):
    def approval_node(state: ApprovalState) -> Command[Literal["execute", "cancel"]]:
        maybe_crash("before_interrupt")
        approved = interrupt(
            {
                "question": "Approve irreversible wire transfer?",
                "order_id": state["order_id"],
                "amount_cents": state["amount_cents"],
            }
        )
        if approved:
            maybe_crash("after_resume_approved_in_approval")
            return Command(goto="execute")
        maybe_crash("after_resume_rejected")
        return Command(goto="cancel")

    def execute_node(state: ApprovalState) -> dict[str, object]:
        maybe_crash("at_execute_entry")
        key = f"wire:{state['order_id']}" if use_idempotency_key else None
        receipt = world.execute_effect(
            op="wire_transfer",
            order_id=state["order_id"],
            amount_cents=state["amount_cents"],
            idempotency_key=key,
        )
        maybe_crash("after_effect_before_checkpoint")
        return {
            "status": "executed",
            "tx_receipt": str(receipt["receipt_id"]),
        }

    def cancel_node(state: ApprovalState) -> dict[str, object]:
        world.execute_effect(
            op="cancelled",
            order_id=state["order_id"],
            amount_cents=0,
            idempotency_key=f"cancel:{state['order_id']}",
        )
        return {"status": "rejected"}

    builder = StateGraph(ApprovalState)
    builder.add_node("approval", approval_node)
    builder.add_node("execute", execute_node)
    builder.add_node("cancel", cancel_node)
    builder.add_edge(START, "approval")
    builder.add_edge("execute", END)
    builder.add_edge("cancel", END)
    return builder.compile(checkpointer=checkpointer)


def _format_result(result: dict[str, object]) -> dict[str, object]:
    interrupts = result.get("__interrupt__")
    interrupted = bool(interrupts)
    return {
        "outcome": "interrupted" if interrupted else "completed",
        "interrupted": interrupted,
        "interrupt_count": len(interrupts) if isinstance(interrupts, (list, tuple)) else 0,
        "status": result.get("status"),
        "tx_receipt": result.get("tx_receipt"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--variant",
        required=True,
        choices=["naive_two_node", "sync_two_node", "mitigated"],
    )
    parser.add_argument(
        "--phase",
        required=True,
        choices=["init", "resume_approve", "resume_reject", "recover"],
    )
    parser.add_argument("--checkpoint-db", required=True)
    parser.add_argument("--world-db", required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--order-id", required=True)
    args = parser.parse_args()

    world = WorldLog(args.world_db)
    conn = sqlite3.connect(args.checkpoint_db, check_same_thread=False)
    try:
        checkpointer = SqliteSaver(conn)
        graph = build_approval_graph(
            checkpointer,
            world,
            use_idempotency_key=(args.variant == "mitigated"),
        )
        config = {"configurable": {"thread_id": args.thread_id}}
        invoke_kwargs: dict[str, object] = {}
        if args.variant in ("sync_two_node", "mitigated"):
            invoke_kwargs["durability"] = "sync"

        if args.phase == "init":
            payload: ApprovalState = {
                "order_id": args.order_id,
                "amount_cents": 50000,
                "status": "pending",
                "tx_receipt": None,
            }
            result = graph.invoke(payload, config, **invoke_kwargs)
            maybe_crash("while_waiting")
            print(json.dumps(_format_result(result)))
        elif args.phase == "resume_approve":
            result = graph.invoke(Command(resume=True), config, **invoke_kwargs)
            print(json.dumps(_format_result(result)))
        elif args.phase == "resume_reject":
            result = graph.invoke(Command(resume=False), config, **invoke_kwargs)
            print(json.dumps(_format_result(result)))
        else:
            try:
                result = graph.invoke(None, config, **invoke_kwargs)
                print(json.dumps(_format_result(result)))
            except EmptyInputError as exc:
                print(json.dumps({"outcome": "empty_input_error", "error": str(exc)}))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
