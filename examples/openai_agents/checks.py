"""shape scenarios for the OpenAI Agents SDK support agent.

    shape test examples/openai_agents/checks.py::refund_unkeyed --seeds 300

The agent code is agent.py, unchanged between variants except for what each variant names.
"""

import asyncio

from agent import (
    SupportContext,
    build_agent,
    handle_message,
    handle_message_with_approval,
    open_session,
)
from payments import PaymentsClient

import shape

MESSAGE = "Please refund order 17."
REFUND_ONCE = [shape.at_most_once("payments.refund"), shape.exactly_once("payments.refund")]
APPROVED = [*REFUND_ONCE, shape.approved_before("payments.refund")]


class TicketUnresolved(Exception):
    pass


def _support(name: str, keyed: bool, approval: bool):
    """One scenario: handle the customer's message. A restarted process runs the same handler
    again with the same session directory, which is how an app resumes a conversation."""
    def scenario_fn(run: shape.Run) -> None:
        ctx = SupportContext(PaymentsClient(run.dir / "payments.db"), key_refunds=keyed)
        session, agent = open_session(run.dir), build_agent(approval)
        if approval:
            reply = handle_message_with_approval(agent, ctx, session, run.dir, MESSAGE)
        else:
            reply = handle_message(agent, ctx, session, MESSAGE)
        answer = asyncio.run(reply)
        print(answer)
        if not answer.startswith("Order 17 is refunded"):
            # the worker only acks a ticket the agent resolved; otherwise the job fails and the
            # queue redelivers it (to a restarted process)
            raise TicketUnresolved(answer)
    scenario_fn.__name__ = name
    return shape.scenario(invariants=APPROVED if approval else REFUND_ONCE,
                          max_restarts=3)(scenario_fn)


refund_unkeyed = _support("refund_unkeyed", keyed=False, approval=False)
refund_keyed = _support("refund_keyed", keyed=True, approval=False)
refund_approval_unkeyed = _support("refund_approval_unkeyed", keyed=False, approval=True)
refund_approval_keyed = _support("refund_approval_keyed", keyed=True, approval=True)
