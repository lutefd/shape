"""The durable store: what survives a crash of the runtime process.

Three tables: `sagas` (one record per saga), `intents` (one per call the runtime means to make,
written before the call), and `audit` (what the runtime did, for humans). Every write goes
through a method here, and each method is a crash point (`crash:durable:<table>#<n>`): the
process may die before the write lands or just after it lands. Rows are copied on the way in
and out, so a write the process never finished cannot leak into the store through a shared
reference.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from ..sim.faults import Crash

RUNNING, COMMITTED, COMPENSATING, COMPENSATED, ESCALATED = (
    "running", "committed", "compensating", "compensated", "escalated",
)
TERMINAL = (COMMITTED, COMPENSATED, ESCALATED)


@dataclass
class Intent:
    saga: str
    step: str  # "reserve", "charge", "email", "comp:charge", "comp:reserve"
    tool: str
    op: str
    args: dict[str, Any]
    key: str | None
    state: str = "started"  # started | done | failed
    result: dict[str, Any] | None = None
    unknown: bool = False  # an attempt timed out or a crash cut it off, so the effect may exist


@dataclass
class SagaRecord:
    id: str
    order: dict[str, Any]  # {"order": 17, "sku": "sku-1", "qty": 2, "amount": 50, "to": "..."}
    state: str = RUNNING
    cursor: int = 0  # policy decisions made so far
    pending: dict[str, Any] | None = None  # the decided action not yet fully handled
    last: dict[str, Any] | None = None  # what the policy is shown next
    conflicts: list[str] = field(default_factory=list)  # records the runtime could not restore


class Store:
    def __init__(self, crash_at: Callable[[str], int] = lambda point: 0) -> None:
        self.sagas: dict[str, SagaRecord] = {}
        self.intents: dict[tuple[str, str], Intent] = {}
        self.audit_log: list[dict[str, Any]] = []
        self.crash_at = crash_at

    def _write(self, table: str, apply: Callable[[], None]) -> None:
        mode = self.crash_at(f"durable:{table}")
        if mode == 1:
            raise Crash(f"before durable write to {table}")
        apply()
        if mode == 2:
            raise Crash(f"after durable write to {table}")

    # sagas
    def put_saga(self, saga: SagaRecord) -> None:
        self._write("sagas", lambda: self.sagas.__setitem__(saga.id, copy.deepcopy(saga)))

    def saga(self, saga_id: str) -> SagaRecord:
        return copy.deepcopy(self.sagas[saga_id])

    def open_sagas(self) -> list[str]:
        return [s.id for s in self.sagas.values() if s.state not in TERMINAL]

    # intents
    def put_intent(self, intent: Intent) -> None:
        self._write("intents", lambda: self.intents.__setitem__(
            (intent.saga, intent.step), copy.deepcopy(intent)))

    def intent(self, saga_id: str, step: str) -> Intent | None:
        found = self.intents.get((saga_id, step))
        return replace(found, args=copy.deepcopy(found.args)) if found else None

    def intents_of(self, saga_id: str) -> list[Intent]:
        return [copy.deepcopy(i) for (s, _), i in self.intents.items() if s == saga_id]

    # audit
    def audit(self, **entry: Any) -> None:
        self._write("audit", lambda: self.audit_log.append(copy.deepcopy(entry)))
