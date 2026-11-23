"""Oracles over the world log: the effects that really happened, as the code under test cannot
misreport them."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Violation:
    invariant: str
    message: str


class World:
    """The merged, ordered world log of one seed plus whether the scenario finished."""

    def __init__(self, entries: list[dict], completed: bool, incarnations: int, max_restarts: int):
        self.entries = sorted(entries, key=lambda e: (e["inc"], e["seq"]))
        self.completed = completed
        self.incarnations = incarnations
        self.max_restarts = max_restarts

    def effects(self, op: str) -> list[dict]:
        """Real effects of `op`. Calls that share a scope and an idempotency key are one."""
        seen: set[tuple[str, str]] = set()
        out = []
        for e in self.entries:
            if e["kind"] != "effect" or e["op"] != op:
                continue
            if e["key"] is not None:
                if (e["scope"], e["key"]) in seen:
                    continue
                seen.add((e["scope"], e["key"]))
            out.append(e)
        return out


@dataclass(frozen=True)
class Invariant:
    name: str
    check: Callable[[World], str | None]


def _duplicates(world: World, op: str) -> str | None:
    by_scope: dict[str, int] = defaultdict(int)
    for e in world.effects(op):
        by_scope[e["scope"]] += 1
    for scope, n in by_scope.items():
        if n > 1:
            where = f"scope {scope!r}" if scope else "the run"
            return f"{op} happened {n} times for {where} (at most 1 allowed)"
    return None


def at_most_once(op: str) -> Invariant:
    return Invariant(f"at_most_once({op})", lambda w: _duplicates(w, op))


def exactly_once(op: str) -> Invariant:
    """At most once per scope, and, once the scenario finished, at least once overall."""
    def check(world: World) -> str | None:
        if (dup := _duplicates(world, op)) is not None:
            return dup
        if world.completed and not world.effects(op):
            return f"{op} never happened, but the scenario finished (a lost effect)"
        return None
    return Invariant(f"exactly_once({op})", check)


def approved_before(op: str) -> Invariant:
    """Every effect of `op` has an earlier approval with the same scope and argument hash."""
    def check(world: World) -> str | None:
        approvals: set[tuple[str, str]] = set()
        for e in world.entries:
            if e["kind"] == "approval":
                approvals.add((e["scope"], e["args_hash"]))
            elif (e["kind"] == "effect" and e["op"] == op
                  and (e["scope"], e["args_hash"]) not in approvals):
                return f"{e['call']} ran with no earlier approval for scope {e['scope']!r}"
        return None
    return Invariant(f"approved_before({op})", check)


def scenario_terminates() -> Invariant:
    def check(world: World) -> str | None:
        if world.completed:
            return None
        errors = [e for e in world.entries if e["kind"] == "error"]
        last = f"; last error: {errors[-1]['text']}" if errors else ""
        return (f"the scenario did not finish within max_restarts={world.max_restarts} "
                f"({world.incarnations} incarnations){last}")
    return Invariant("scenario_terminates", check)
