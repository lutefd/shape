"""World: the god's-eye record of what really happened, separate from what the agent or its
audit log believes. Tools mutate it; invariants read it. It lives outside the runtime, so a
crash of the system under test never touches it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

EXTERNAL = "external"


def digest(tool: str, op: str, args: dict[str, Any]) -> str:
    """Canonical identity of one action. The approval gate and the audit log match on it."""
    return f"{tool}.{op}:{json.dumps(args, sort_keys=True, default=str)}"


@dataclass(frozen=True)
class Effect:
    tool: str
    op: str
    scope: str  # the business object acted on, e.g. "order:17/charge"
    key: str | None  # idempotency key the caller sent, if any
    reversible: bool
    args: dict[str, Any]
    t: float


@dataclass
class Record:
    value: dict[str, Any]
    version: int = 1


@dataclass
class Charge:
    order: int
    amount: int
    key: str | None
    refunded: bool = False


@dataclass(frozen=True)
class Gate:
    """A human approval that was granted: for exactly this action, after this many effects."""

    request: str
    action: str  # digest(...)
    after_effects: int


@dataclass
class World:
    now: Callable[[], float] = lambda: 0.0
    effects: list[Effect] = field(default_factory=list)
    records: dict[str, Record] = field(default_factory=dict)
    before: dict[str, dict[str, Any]] = field(default_factory=dict)
    external_stock: dict[str, int] = field(default_factory=dict)  # net stock added by outsiders
    charges: dict[str, Charge] = field(default_factory=dict)
    idempotency: dict[tuple[str, str], tuple[str, dict[str, Any]]] = field(default_factory=dict)
    outbox: list[dict[str, Any]] = field(default_factory=list)
    gates: list[Gate] = field(default_factory=list)

    def seed_record(self, record_id: str, value: dict[str, Any]) -> None:
        self.records[record_id] = Record(json.loads(json.dumps(value)))
        self.before[record_id] = json.loads(json.dumps(value))

    def effect(self, tool: str, op: str, scope: str, key: str | None, reversible: bool,
               args: dict[str, Any]) -> None:
        self.effects.append(Effect(tool, op, scope, key, reversible, dict(args), self.now()))

    def external_edit(self, record_id: str, stock_delta: int) -> None:
        """An outside actor changes a record without going through the agent."""
        rec = self.records[record_id]
        rec.value["stock"] += stock_delta
        rec.version += 1
        self.external_stock[record_id] = self.external_stock.get(record_id, 0) + stock_delta
        self.effect(EXTERNAL, "edit", f"db:{record_id}", None, True, {"stock_delta": stock_delta})

    def snapshot(self) -> str:
        """Everything an idempotent compensation must leave unchanged."""
        return json.dumps(
            {
                "records": {k: [r.version, r.value] for k, r in sorted(self.records.items())},
                "charges": {k: vars(c) for k, c in sorted(self.charges.items())},
                "outbox": self.outbox,
                "effects": len(self.effects),
            },
            sort_keys=True,
        )
