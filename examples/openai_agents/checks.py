"""shape scenarios for the OpenAI Agents SDK support agent, using `shape.agents`.

    shape test examples/openai_agents/checks.py::refund_unkeyed --seeds 300
"""

from __future__ import annotations

import json

from agent import TICKET, SupportContext, build_agent, build_tools, refund_key
from agents import SQLiteSession
from payments import PaymentsClient

import shape
import shape.agents

MESSAGE = "Please refund order 17."
PLAN = [
    ("lookup_order", {"order_id": "17"}),
    ("issue_refund", {"order_id": "17", "amount_cents": 4200}),
]
REFUND_ONCE = [shape.at_most_once("payments.refund"), shape.exactly_once("payments.refund")]
APPROVED = [*REFUND_ONCE, shape.approved_before("payments.refund")]


class TicketUnresolved(Exception):
    pass


def _approve(item) -> bool:
    args = json.loads(item.arguments)
    shape.approval(f"order:{args['order_id']}", args)
    return True


def _run_refund(
    run: shape.Run,
    *,
    keyed: bool,
    approval: bool = False,
    alternatives: dict | None = None,
    allowed_orders: frozenset[str] | None = None,
    require_refund: bool = True,
) -> None:
    payments = PaymentsClient(run.dir / "payments.db")
    ctx = SupportContext(payments, key_refunds=keyed, allowed_orders=allowed_orders)
    tools = shape.agents.effects(
        build_tools(approval=approval),
        ops={
            "issue_refund": {
                "op": "payments.refund",
                "scope": lambda order_id, **_: f"order:{order_id}",
                "key": lambda order_id, **_: refund_key(order_id) if keyed else None,
            }
        },
    )
    model = shape.agents.ScriptedModel(
        PLAN, final="Order 17 is refunded.", alternatives=alternatives
    )
    agent = build_agent(model, tools=tools)
    shape.agents.drive(
        agent,
        MESSAGE,
        run,
        context=ctx,
        session_factory=lambda d: SQLiteSession(TICKET, str(d / "session.db")),
        human=_approve if approval else None,
        on_restart="redeliver",
    )
    if require_refund and not payments.has_refund("17"):
        raise TicketUnresolved("order 17 was not refunded")


@shape.scenario(invariants=REFUND_ONCE, max_restarts=3)
def refund_unkeyed(run: shape.Run) -> None:
    _run_refund(run, keyed=False)


@shape.scenario(invariants=REFUND_ONCE, max_restarts=3)
def refund_keyed(run: shape.Run) -> None:
    _run_refund(run, keyed=True)


@shape.scenario(invariants=APPROVED, max_restarts=3)
def refund_approval_unkeyed(run: shape.Run) -> None:
    _run_refund(run, keyed=False, approval=True)


@shape.scenario(invariants=APPROVED, max_restarts=3)
def refund_approval_keyed(run: shape.Run) -> None:
    _run_refund(run, keyed=True, approval=True)


@shape.scenario(
    invariants=[shape.at_most_once("payments.refund"), shape.approved_before("payments.refund")],
    max_restarts=3,
)
def refund_wrong_order_unguarded(run: shape.Run) -> None:
    """Model perturbation (`substitute order_id='18'`) refunds the wrong order with zero faults."""
    shape.approval("order:17", {"order_id": "17", "amount_cents": 4200})
    _run_refund(run, keyed=True, alternatives={"order_id": ["18"]}, require_refund=False)


@shape.scenario(
    invariants=[shape.at_most_once("payments.refund"), shape.approved_before("payments.refund")],
    max_restarts=3,
)
def refund_wrong_order_guarded(run: shape.Run) -> None:
    """Allowlist guard inside `issue_refund` blocks the perturbed order_id='18' call."""
    shape.approval("order:17", {"order_id": "17", "amount_cents": 4200})
    _run_refund(
        run,
        keyed=True,
        alternatives={"order_id": ["18"]},
        allowed_orders=frozenset({"17"}),
        require_refund=False,
    )
