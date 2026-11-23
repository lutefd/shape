"""LangGraph interrupt()-based human approval scenarios, using `shape.langgraph.drive`.

Run with:
    shape test examples/langgraph/approval.py --seeds 200 --jobs 4
"""

from __future__ import annotations

from typing import Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from payments import TransferProvider

import shape
import shape.langgraph

THREAD_ID = "wire-7"
INIT_STATE = {
    "thread_id": THREAD_ID,
    "order_id": "ord-7",
    "to": "acct-42",
    "amount": 50000,
    "status": "pending",
    "transfer_id": None,
}
CONFIG = {"configurable": {"thread_id": THREAD_ID}}
APPROVED_ONCE = [
    shape.approved_before("transfers.send"),
    shape.at_most_once("transfers.send"),
    shape.exactly_once("transfers.send"),
]


class TransferState(TypedDict):
    thread_id: str
    order_id: str
    to: str
    amount: int
    status: Literal["pending", "sent", "rejected"]
    transfer_id: int | None


def _run_approval_graph(
    run: shape.Run,
    *,
    decision: bool,
    durability: str | None,
    use_idempotency_key: bool,
) -> None:
    provider = TransferProvider(run.dir / "provider.db")

    @shape.effect(
        "transfers.send",
        scope=lambda order_id, **_: f"order:{order_id}",
        key=lambda idempotency_key=None, **_: idempotency_key,
        irreversible=True,
    )
    def send(order_id: str, to: str, amount: int, idempotency_key: str | None = None) -> int:
        return provider.send(order_id, to, amount, idempotency_key)

    try:
        with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as checkpointer:
            def approval_node(state: TransferState) -> Command[Literal["send", "cancel"]]:
                approved = interrupt(
                    {
                        "question": "Approve irreversible wire transfer?",
                        "order_id": state["order_id"],
                        "to": state["to"],
                        "amount": state["amount"],
                    }
                )
                if approved:
                    key = f"{state['thread_id']}:send" if use_idempotency_key else None
                    shape.approval(
                        f"order:{state['order_id']}",
                        {
                            "order_id": state["order_id"],
                            "to": state["to"],
                            "amount": state["amount"],
                            "idempotency_key": key,
                        },
                    )
                    return Command(goto="send")
                return Command(goto="cancel")

            def send_node(state: TransferState) -> dict[str, object]:
                key = f"{state['thread_id']}:send" if use_idempotency_key else None
                tid = send(state["order_id"], state["to"], state["amount"], key)
                return {"status": "sent", "transfer_id": tid}

            builder = StateGraph(TransferState)
            builder.add_node("approval", approval_node)
            builder.add_node("send", send_node)
            builder.add_node("cancel", lambda _s: {"status": "rejected"})
            builder.add_edge(START, "approval")
            builder.add_edge("send", END)
            builder.add_edge("cancel", END)
            graph = builder.compile(checkpointer=checkpointer)
            shape.langgraph.drive(
                graph,
                INIT_STATE,
                CONFIG,
                run,
                human=lambda _payload: decision,
                durability=durability,
            )
    finally:
        provider.close()


@shape.scenario(invariants=[shape.approved_before("transfers.send")], max_restarts=3)
def approval_gate_only(run: shape.Run) -> None:
    """Docs-style interrupt() approval, default durability='async', checking approved_before only."""
    _run_approval_graph(run, decision=True, durability=None, use_idempotency_key=False)


@shape.scenario(invariants=APPROVED_ONCE, max_restarts=3)
def approval_default(run: shape.Run) -> None:
    """Docs-style interrupt() approval, default durability='async', no idempotency key."""
    _run_approval_graph(run, decision=True, durability=None, use_idempotency_key=False)


@shape.scenario(invariants=APPROVED_ONCE, max_restarts=3)
def approval_sync_fixed(run: shape.Run) -> None:
    """Hardened interrupt() approval: durability='sync' + deterministic idempotency key."""
    _run_approval_graph(run, decision=True, durability="sync", use_idempotency_key=True)


@shape.scenario(invariants=[shape.approved_before("transfers.send")], max_restarts=3)
def approval_rejected(run: shape.Run) -> None:
    """Rejected transfer: human resumes with False; send must never run."""
    _run_approval_graph(run, decision=False, durability="sync", use_idempotency_key=True)
