"""A support agent written the way the Agents SDK docs write one (`@function_tool`, `Agent`).

Zero shape imports and zero test-harness code: `checks.py` plugs in `shape.agents.effects`,
`shape.agents.ScriptedModel`, and `shape.agents.drive` from outside.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from agents import Agent, Model, RunContextWrapper, function_tool
from payments import PaymentsClient

TICKET = "ticket-17"


@dataclass
class SupportContext:
    payments: PaymentsClient
    key_refunds: bool = False
    allowed_orders: frozenset[str] | None = None


def refund_key(order_id: str) -> str:
    """One refund per order per ticket, derived from durable business facts."""
    return f"{TICKET}:refund:{order_id}"


@function_tool
def lookup_order(ctx: RunContextWrapper[SupportContext], order_id: str) -> str:
    """Look up an order: its status and total in cents."""
    order = ctx.context.payments.lookup_order(order_id)
    return json.dumps({"order_id": order_id, **order}) if order else "no such order"


async def issue_refund(
    ctx: RunContextWrapper[SupportContext], order_id: str, amount_cents: int
) -> str:
    """Refund an order to the customer's original payment method."""
    if ctx.context.allowed_orders is not None and order_id not in ctx.context.allowed_orders:
        raise PermissionError(f"order {order_id} is not authorized for this ticket")
    key = refund_key(order_id) if ctx.context.key_refunds else None
    refund_id = await ctx.context.payments.refund(order_id, amount_cents, idempotency_key=key)
    return json.dumps({"refund_id": refund_id, "order_id": order_id})


def build_tools(*, approval: bool = False) -> list:
    return [lookup_order, function_tool(issue_refund, needs_approval=approval)]


def build_agent(model: Model, *, tools: list | None = None, approval: bool = False) -> Agent:
    return Agent(
        name="Support",
        instructions="You are a support agent. Refund orders on request.",
        model=model,
        tools=tools if tools is not None else build_tools(approval=approval),
    )
