"""A support agent written the way the Agents SDK docs write one: `@function_tool` tools, an
`Agent`, `Runner.run`, a `SQLiteSession` for conversation memory.

Nothing in this file imports shape except through `payments.py`, where the one call that moves
money carries `@shape.effect`. `scripted_model.py` stands in for the LLM so the tests need no key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agents import (
    Agent,
    RunContextWrapper,
    Runner,
    RunState,
    SQLiteSession,
    function_tool,
    set_tracing_disabled,
)
from payments import PaymentsClient
from scripted_model import ScriptedSupportModel

import shape

set_tracing_disabled(True)  # tracing exports to the OpenAI platform, which needs a key

TICKET = "ticket-17"


@dataclass
class SupportContext:
    payments: PaymentsClient
    key_refunds: bool = False  # the fix: send an idempotency key derived from durable state


@function_tool
def lookup_order(ctx: RunContextWrapper[SupportContext], order_id: str) -> str:
    """Look up an order: its status and total in cents."""
    order = ctx.context.payments.lookup_order(order_id)
    return json.dumps({"order_id": order_id, **order}) if order else "no such order"


def refund_key(order_id: str) -> str:
    """One refund per order per ticket. It comes from the ticket and the order, which are the
    durable business facts, not from anything the model or the run generates (call ids change
    when the model re-issues a call)."""
    return f"{TICKET}:refund:{order_id}"


def refund_args(order_id: str, amount_cents: int, keyed: bool) -> dict:
    return {"order_id": order_id, "amount_cents": amount_cents,
            "idempotency_key": refund_key(order_id) if keyed else None}


async def issue_refund(ctx: RunContextWrapper[SupportContext], order_id: str,
                       amount_cents: int) -> str:
    """Refund an order to the customer's original payment method."""
    args = refund_args(order_id, amount_cents, ctx.context.key_refunds)
    refund_id = await ctx.context.payments.refund(**args)
    return json.dumps({"refund_id": refund_id, "order_id": order_id})


def build_agent(approval: bool = False) -> Agent[SupportContext]:
    """`approval=True` is the SDK's human-in-the-loop flow: the refund tool needs approval."""
    refund = function_tool(issue_refund, needs_approval=approval)
    return Agent(name="Support", instructions="You are a support agent. Refund orders on request.",
                 model=ScriptedSupportModel(), tools=[lookup_order, refund])


def open_session(workdir: Path) -> SQLiteSession:
    return SQLiteSession(TICKET, str(workdir / "session.db"))


async def handle_message(agent: Agent, ctx: SupportContext, session: SQLiteSession,
                         text: str) -> str:
    result = await Runner.run(agent, text, context=ctx, session=session)
    return result.final_output


def save(state: RunState) -> str:
    # the payments client is rebuilt on load (context_override), so only the flag is stored
    return state.to_string(context_serializer=lambda c: {"key_refunds": c.key_refunds})


async def handle_message_with_approval(agent: Agent, ctx: SupportContext, session: SQLiteSession,
                                       workdir: Path, text: str) -> str:
    """The SDK's human-in-the-loop flow (https://openai.github.io/openai-agents-python/human_in_the_loop/),
    with the paused `RunState` saved to disk the way that page shows, so a restarted process can
    pick it up. The approver here is scripted and always says yes, after recording the approval."""
    path = workdir / "paused-run.json"
    if path.exists():
        state = await RunState.from_json(agent, json.loads(path.read_text()), context_override=ctx)
    else:
        result = await Runner.run(agent, text, context=ctx, session=session)
        if not result.interruptions:
            return result.final_output
        state = result.to_state()
        path.write_text(save(state))
    while True:
        for item in state.get_interruptions():
            args = json.loads(item.arguments)
            shape.approval(f"order:{args['order_id']}",
                           refund_args(args["order_id"], args["amount_cents"], ctx.key_refunds))
            state.approve(item)
        result = await Runner.run(agent, state, session=session)
        if not result.interruptions:
            path.unlink()
            return result.final_output
        state = result.to_state()
        path.write_text(save(state))
