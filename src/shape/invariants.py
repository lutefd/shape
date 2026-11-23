"""Invariants: oracles over what really happened.

Each takes an `Outcome` (the god's-eye world, the runtime's durable store, the trace) and
returns messages, one per violation. They read the World, never the runtime's own account of
itself, so a runtime that believes it behaved cannot vouch for itself.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from .runtime.store import COMPENSATED, ESCALATED, TERMINAL, Store
from .sim.world import EXTERNAL, World, digest
from .trace import Trace


@dataclass
class Outcome:
    world: World
    store: Store
    trace: Trace
    stuck: str | None = None  # the loop gave up: deadlock or time limit
    recompensation: str | None = None  # how the world changed when compensations ran again


@dataclass(frozen=True)
class Invariant:
    name: str
    check: Callable[[Outcome], list[str]]


@dataclass(frozen=True)
class Violation:
    invariant: str
    message: str


def at_most_once_effect(o: Outcome) -> list[str]:
    seen = Counter((e.tool, e.op, e.scope) for e in o.world.effects if e.tool != EXTERNAL)
    return [f"{n} {tool}.{op} effects for {scope}" for (tool, op, scope), n in seen.items() if n > 1]


def gate_before_irreversible(o: Outcome) -> list[str]:
    out = []
    for i, e in enumerate(o.world.effects):
        if e.reversible or e.tool == EXTERNAL:
            continue
        action = digest(e.tool, e.op, e.args)
        if not any(g.action == action and g.after_effects <= i for g in o.world.gates):
            out.append(f"{e.tool}.{e.op} for {e.scope} happened with no earlier approval of it")
    return out


def rollback_restores_state(o: Outcome) -> list[str]:
    out = []
    for saga in o.store.sagas.values():
        if saga.state != COMPENSATED:
            continue
        order = saga.order["order"]
        for record_id, before in o.world.before.items():
            if record_id in saga.conflicts:
                continue
            want = {"stock": before["stock"] + o.world.external_stock.get(record_id, 0),
                    "held": before["held"]}
            if o.world.records[record_id].value != want:
                out.append(f"{saga.id} compensated but {record_id} is "
                           f"{o.world.records[record_id].value}, expected {want}")
        out += [f"{saga.id} compensated but charge {cid} is not refunded"
                for cid, c in o.world.charges.items() if c.order == order and not c.refunded]
        out += [f"{saga.id} compensated but {e.tool}.{e.op} for {e.scope} cannot be undone"
                for e in o.world.effects
                if not e.reversible and e.scope.startswith(f"order:{order}/")]
    return out


def no_invisible_effects(o: Outcome) -> list[str]:
    audited = {digest(a["tool"], a["op"], a["args"]) for a in o.store.audit_log
               if a.get("kind") == "call"}
    return [f"{e.tool}.{e.op} for {e.scope} is missing from the audit log"
            for e in o.world.effects
            if e.tool != EXTERNAL and digest(e.tool, e.op, e.args) not in audited]


def compensation_idempotent(o: Outcome) -> list[str]:
    return [o.recompensation] if o.recompensation else []


def saga_terminates(o: Outcome) -> list[str]:
    out = [f"run did not finish: {o.stuck}"] if o.stuck else []
    # The client's own record, not the store's: an order that never reached a durable row is
    # invisible to a check over persisted sagas and would pass vacuously.
    persisted = {s.order["order"] for s in o.store.sagas.values()}
    out += [f"order {order['order']} was submitted but no saga for it was ever persisted"
            for order in o.world.submitted if order["order"] not in persisted]
    for saga in o.store.sagas.values():
        if saga.state not in TERMINAL:
            out.append(f"{saga.id} ended in state {saga.state!r}")
        elif saga.state == ESCALATED and not any(
                a.get("kind") == "escalate" and a.get("saga") == saga.id for a in o.store.audit_log):
            out.append(f"{saga.id} escalated without telling anyone")
    return out


ALL = [
    Invariant(f.__name__, f)
    for f in (at_most_once_effect, gate_before_irreversible, rollback_restores_state,
              no_invisible_effects, compensation_idempotent, saga_terminates)
]


def evaluate(invariants: list[Invariant], outcome: Outcome) -> list[Violation]:
    return [Violation(inv.name, m) for inv in invariants for m in inv.check(outcome)]
