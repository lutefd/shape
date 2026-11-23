"""LangGraph tool-calling refund agent using `shape.langgraph` (`effects`, `ScriptedChatModel`,
`drive`).

Demonstrates model-perturbation failures (no injected fault needed) and how user guards fix them:
- `refund_repeat_unguarded`: model repeats `issue_refund` -> fails `at_most_once` with 0 faults.
- `refund_repeat_guarded`: idempotency key derived from thread+order deduplicates repeats -> PASS.
- `refund_wrong_order_unguarded`: model substitutes `order_id='18'` -> fails `approved_before`.
- `refund_wrong_order_guarded`: allowlist check inside `issue_refund` blocks wrong order -> PASS.

Run with:
    shape test examples/langgraph/refund_agent.py --seeds 200 --jobs 4
"""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from payments import RefundProvider

import shape
import shape.langgraph

THREAD_ID = "ticket-17"
CONFIG = {"configurable": {"thread_id": THREAD_ID}}
PLAN = [
    ("lookup_order", {"order_id": "17"}),
    ("issue_refund", {"order_id": "17", "amount_cents": 4200}),
]


def _run_refund_agent(
    run: shape.Run,
    *,
    keyed: bool,
    alternatives: dict | None = None,
    allowed_orders: frozenset[str] | None = None,
) -> None:
    provider = RefundProvider(run.dir / "provider.db")

    @tool
    def lookup_order(order_id: str) -> str:
        """Look up an order's status and total."""
        order = provider.lookup(order_id)
        return json.dumps({"order_id": order_id, **order}) if order else "no such order"

    @tool
    def issue_refund(order_id: str, amount_cents: int) -> str:
        """Refund an order to the customer's payment method."""
        if allowed_orders is not None and order_id not in allowed_orders:
            raise PermissionError(f"order {order_id} is not authorized")
        key = f"{THREAD_ID}:refund:{order_id}" if keyed else None
        return json.dumps({"refund_id": provider.refund(order_id, amount_cents, key)})

    tools = shape.langgraph.effects(
        [lookup_order, issue_refund],
        ops={
            "issue_refund": {
                "op": "payments.refund",
                "scope": lambda order_id, **_: f"order:{order_id}",
                "key": lambda order_id, **_: f"{THREAD_ID}:refund:{order_id}" if keyed else None,
            }
        },
    )
    model = shape.langgraph.ScriptedChatModel(
        PLAN, final="Order 17 is refunded.", alternatives=alternatives
    ).bind_tools(tools)

    try:
        with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as checkpointer:
            builder = StateGraph(MessagesState)
            builder.add_node("agent", lambda s: {"messages": [model.invoke(s["messages"])]})
            builder.add_node("tools", ToolNode(tools, handle_tool_errors=True))
            builder.add_edge(START, "agent")
            builder.add_conditional_edges("agent", tools_condition)
            builder.add_edge("tools", "agent")
            graph = builder.compile(checkpointer=checkpointer)
            shape.langgraph.drive(
                graph,
                {"messages": [HumanMessage(content="Please refund order 17.")]},
                CONFIG,
                run,
                durability="sync",
            )
    finally:
        provider.close()


@shape.scenario(invariants=[shape.at_most_once("payments.refund")], max_restarts=3)
def refund_repeat_unguarded(run: shape.Run) -> None:
    """No idempotency key: model perturbation `repeat` (or post-commit fault) double-refunds."""
    _run_refund_agent(run, keyed=False)


@shape.scenario(invariants=[shape.at_most_once("payments.refund")], max_restarts=3)
def refund_repeat_guarded(run: shape.Run) -> None:
    """Idempotency key derived from thread + order absorbs model repeats and retries."""
    _run_refund_agent(run, keyed=True)


@shape.scenario(
    invariants=[shape.at_most_once("payments.refund"), shape.approved_before("payments.refund")],
    max_restarts=3,
)
def refund_wrong_order_unguarded(run: shape.Run) -> None:
    """Model perturbation `substitute order_id='18'` refunds an unapproved order (zero faults)."""
    shape.approval("order:17", {"order_id": "17", "amount_cents": 4200})
    _run_refund_agent(run, keyed=True, alternatives={"order_id": ["18"]})


@shape.scenario(
    invariants=[shape.at_most_once("payments.refund"), shape.approved_before("payments.refund")],
    max_restarts=3,
)
def refund_wrong_order_guarded(run: shape.Run) -> None:
    """Allowlist check inside `issue_refund` blocks the perturbed order_id='18' call."""
    shape.approval("order:17", {"order_id": "17", "amount_cents": 4200})
    _run_refund_agent(
        run,
        keyed=True,
        alternatives={"order_id": ["18"]},
        allowed_orders=frozenset({"17"}),
    )
