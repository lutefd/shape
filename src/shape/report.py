"""Turn a failing run into the few lines a human reads."""

from __future__ import annotations

from .invariants import Violation
from .simulation import RunResult

STORY_KINDS = ("fault", "crash", "actor", "human", "retry", "sched")


def story(result: RunResult, violation: Violation) -> list[str]:
    """The deviations that mattered, each with the first call they hit, then the outcome."""
    events = result.trace.events
    hit = {(e.data["tool"], e.data["op"]) for e in events
           if e.kind in ("fault", "retry")}
    lines: list[str] = []
    first_call: set[tuple[str, str]] = set()
    for e in events:
        if e.kind == "call":
            ident = (e.data["tool"], e.data["op"])
            if ident in hit and ident not in first_call:
                first_call.add(ident)
                lines.append(f"call: {e.text}")
        elif e.kind in STORY_KINDS and e.data.get("outcome", 1):
            lines.append(f"{e.kind}: {e.text}")
        elif e.kind == "agent" and f"agent#{e.data['step']}" in result.realized:
            lines.append(f"agent: {e.text}")
    lines.append(f"world: {violation.message}")
    return lines


def render_story(lines: list[str]) -> str:
    return "\n".join(f"  {i}. {line}" for i, line in enumerate(lines, 1))
