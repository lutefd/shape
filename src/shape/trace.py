"""Event and Trace: the append-only record of what happened in one run.

The trace is the run's observable behavior. Two runs are the same run exactly when their
traces have the same `fingerprint`. Event kinds: `agent` (a policy decision), `call` (a tool
call leaving the runtime), `fault`, `crash`, `actor` (external modification), `human`,
`retry`, `world` (a real effect landing), `sched` (a non-FIFO scheduling pick), `runtime`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    seq: int
    t: float
    kind: str
    text: str
    data: dict[str, Any] = field(default_factory=dict)


class Trace:
    def __init__(self, clock: Callable[[], float] = lambda: 0.0) -> None:
        self.clock = clock
        self.events: list[Event] = []

    def add(self, kind: str, text: str, **data: Any) -> Event:
        event = Event(len(self.events), round(self.clock(), 6), kind, text, data)
        self.events.append(event)
        return event

    def fingerprint(self) -> tuple:
        return tuple((e.t, e.kind, e.text, repr(sorted(e.data.items()))) for e in self.events)

    def render(self) -> str:
        return "\n".join(f"{e.seq:4d} t={e.t:<8.3f} {e.kind:<8} {e.text}" for e in self.events)
