"""Oracles over the world log: the effects that really happened, as the code under test cannot
misreport them."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Self

from .log import args_hash


@dataclass(frozen=True)
class Violation:
    invariant: str
    message: str
    op: str | None = None


class _OpMessage(str):
    """A plain violation message string that also carries the op that violated the invariant."""

    op: str

    def __new__(cls, text: str, op: str) -> Self:
        obj = super().__new__(cls, text)
        obj.op = op
        return obj


def _identities(e: dict) -> list[tuple]:
    """What makes two logged effects one: the same op, scope and idempotency key (the provider
    dedupes by key), or, for an `idempotent=True` effect, the same op, scope and arguments."""
    ids = []
    if e["key"] is not None:
        ids.append((e["op"], e["scope"], "key", e["key"]))
    if e.get("idempotent"):
        ids.append((e["op"], e["scope"], "args", e["args_hash"]))
    return ids


class World:
    """What really happened, as the code under test cannot misreport it. A custom invariant
    (`@shape.invariant`) reads it. Entries are plain dicts in the order they were logged.

    - `effects(op=None, scope=None)`: real effects, calls that are one effect counted once. Keys:
      `op`, `scope`, `key`, `call` (rendered call), `args` (named arguments, hidden ones left
      out), `result` (what `result=` returned), `fault` (the fault injected on that call, if
      any; after-commit faults still landed), `inc` (incarnation).
    - `attempts(op=None, scope=None)`: every call, failed and killed ones included. Keys: `op`,
      `scope`, `call`, `fault`, `inc`.
    - `approvals`: `shape.approval(...)` entries (`scope`, `args_hash`).
    - `claims`: `shape.claim(...)` entries (`scope`, `op`).
    - `completed`: the scenario returned. `run`: the final `shape.Run`, so you can read `run.dir`.
    """

    def __init__(self, entries: list[dict], completed: bool, incarnations: int, max_restarts: int,
                 run: Any = None):
        self.entries = entries
        self.completed = completed
        self.incarnations = incarnations
        self.max_restarts = max_restarts
        self.run = run

    def _kind(self, kind: str, op: str | None, scope: str | None) -> list[dict]:
        return [e for e in self.entries if e["kind"] == kind
                and (op is None or e["op"] == op) and (scope is None or e["scope"] == scope)]

    def effects(self, op: str | None = None, scope: str | None = None) -> list[dict]:
        seen: set[tuple] = set()
        out = []
        for e in self.entries:
            if e["kind"] != "effect":
                continue
            ids = _identities(e)
            if any(i in seen for i in ids):
                continue
            seen.update(ids)
            if (op is None or e["op"] == op) and (scope is None or e["scope"] == scope):
                out.append(e)
        return out

    def attempts(self, op: str | None = None, scope: str | None = None) -> list[dict]:
        resolved = {e["attempt_key"]: (e["op"], e["scope"]) for e in self.entries
                    if e["kind"] in {"effect", "no_effect"} and "attempt_key" in e}
        if not resolved:
            return self._kind("attempt", op, scope)
        attempts = []
        for entry in self._kind("attempt", None, None):
            result = resolved.get(entry.get("attempt_key"))
            if result is not None:
                entry = {**entry, "op": result[0], "scope": result[1]}
            if (op is None or entry["op"] == op) and (scope is None or entry["scope"] == scope):
                attempts.append(entry)
        return attempts

    @property
    def approvals(self) -> list[dict]:
        return [e for e in self.entries if e["kind"] == "approval"]

    @property
    def claims(self) -> list[dict]:
        return [e for e in self.entries if e["kind"] == "claim"]


@dataclass(frozen=True)
class Invariant:
    name: str
    check: Callable[[World], str | None]
    ops: tuple[str, ...] = ()  # the effect ops it watches, for the coverage report


def invariant(fn: Callable[[World], str | None]) -> Invariant:
    """Turn `fn(world) -> str | None` into an invariant: return a plain-words message when the
    run is wrong, None when it is fine. Use it in `@shape.scenario(invariants=[...])`."""
    return Invariant(fn.__name__, fn)


def _where(scope: str) -> str:
    return f"scope {scope!r}" if scope else "the run"


def _repeated(world: World, op: str) -> tuple[str, int] | None:
    by_scope: dict[str, int] = defaultdict(int)
    for e in world.effects(op):
        by_scope[e["scope"]] += 1
    return next(((scope, n) for scope, n in by_scope.items() if n > 1), None)


def at_most_once(op: str) -> Invariant:
    def check(world: World) -> str | None:
        if (hit := _repeated(world, op)) is not None:
            return f"{op} happened {hit[1]} times for {_where(hit[0])} (at most 1 allowed)"
        return None
    return Invariant(f"at_most_once({op})", check, (op,))


def exactly_once(op: str, *, failure_claim_ok: bool = False) -> Invariant:
    """At most once per scope, and, once the scenario finished, at least once overall.

    With `failure_claim_ok=True`, a lost effect is acceptable when the agent explicitly recorded a
    failure claim (`shape.claim(scope, op, ok=False)`) for every attempted scope (or any scope if
    none was attempted) and never claimed success (`ok=True`). Still a violation if the agent
    claimed success or finished without reporting failure.
    """
    def check(world: World) -> str | None:
        if (hit := _repeated(world, op)) is not None:
            return f"{op} happened {hit[1]} times for {_where(hit[0])} (it must happen exactly once)"
        if world.completed and not world.effects(op):
            if failure_claim_ok:
                op_claims = [c for c in world.claims if c["op"] == op]
                pos = [c for c in op_claims if c.get("ok", True)]
                if pos:
                    return (f"{op} never happened, but the agent claimed it did for "
                            f"{_where(pos[0]['scope'])}")
                neg_scopes = {c["scope"] for c in op_claims if not c.get("ok", True)}
                attempted_scopes = {a["scope"] for a in world.attempts(op)}
                if attempted_scopes:
                    missing = sorted(attempted_scopes - neg_scopes)
                    if not missing:
                        return None
                    return (f"{op} never happened for {_where(missing[0])}, and the agent "
                            f"recorded no failure claim before finishing (a lost effect)")
                if neg_scopes:
                    return None
                return (f"{op} never happened, and the agent recorded no failure claim before "
                        f"finishing (a lost effect)")
            return f"{op} never happened, but the scenario finished (a lost effect)"
        return None
    return Invariant(f"exactly_once({op})", check, (op,))


def approved_before(op: str, match: Callable[[dict], Any] | None = None) -> Invariant:
    """Every effect of `op` has an earlier approval with the same scope and argument hash.

    `match(effect_args)` maps the effect's named arguments to what the human approved, for when
    the two differ (the effect sits at an API layer below the tool the human saw)."""
    def check(world: World) -> str | None:
        approvals: set[tuple[str, str]] = set()
        for e in world.entries:
            if e["kind"] == "approval":
                approvals.add((e["scope"], e["args_hash"]))
            elif e["kind"] == "effect" and e["op"] == op:
                digest = args_hash(match(e["args"])) if match else e["args_hash"]
                if (e["scope"], digest) not in approvals:
                    return f"{e['call']} ran with no earlier approval for scope {e['scope']!r}"
        return None
    return Invariant(f"approved_before({op})", check, (op,))


def requires(op: str, before: str, same_scope: bool = True) -> Invariant:
    """Every `op` effect has an earlier effect of `before` that landed, for the same scope."""
    def check(world: World) -> str | None:
        landed: set[str] = set()
        for e in world.entries:
            if e["kind"] != "effect":
                continue
            if e["op"] == before:
                landed.add(e["scope"])
            elif e["op"] == op and not (e["scope"] in landed if same_scope else landed):
                where = f" for {_where(e['scope'])}" if same_scope else ""
                return f"{e['call']} happened with no earlier {before} that landed{where}"
        return None
    return Invariant(f"requires({op}, before={before})", check, (op,))


def within_scopes(op: str, allowed: Any) -> Invariant:
    """Every `op` effect acts on an allowed scope. `allowed` is a collection of scopes, or a
    function of the final `shape.Run` that returns one."""
    def check(world: World) -> str | None:
        ok = allowed(world.run) if callable(allowed) else allowed
        for e in world.effects(op):
            if e["scope"] not in ok:
                shown = ", ".join(sorted(map(repr, ok))[:5]) or "none"
                return f"{e['call']} acted on {e['scope']!r}, outside the allowed scopes ({shown})"
        return None
    return Invariant(f"within_scopes({op})", check, (op,))


def _grouped(entries: list[dict], per_scope: bool) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        groups[e["scope"] if per_scope else ""].append(e)
    return groups


def max_calls(op: str, n: int, per_scope: bool = True) -> Invariant:
    """At most `n` calls of `op` (per scope), failed and killed calls included."""
    def check(world: World) -> str | None:
        for scope, calls in _grouped(world.attempts(op), per_scope).items():
            if len(calls) > n:
                return (f"{op} was called {len(calls)} times for {_where(scope)}, failed calls "
                        f"included (at most {n} allowed)")
        return None
    return Invariant(f"max_calls({op}, {n})", check, (op,))


def sum_le(op: str, field: str, limit: float, per_scope: bool = True) -> Invariant:
    """The numeric argument `field` of real `op` effects sums to at most `limit` (per scope)."""
    def check(world: World) -> str | None:
        for scope, hits in _grouped(world.effects(op), per_scope).items():
            total = sum(v for e in hits if isinstance(v := e["args"].get(field), int | float))
            if total > limit:
                return f"{op} {field} added up to {total} for {_where(scope)} (limit {limit})"
        return None
    return Invariant(f"sum_le({op}.{field}, {limit})", check, (op,))


def claims_true() -> Invariant:
    """Every `shape.claim(scope, op, ok=True)` has an earlier effect of `op` for `scope` that
    landed, and every `shape.claim(scope, op, ok=False)` has no earlier effect that landed."""
    def check(world: World) -> str | None:
        landed: set[tuple[str, str]] = set()
        for e in world.entries:
            if e["kind"] == "effect":
                landed.add((e["op"], e["scope"]))
            elif e["kind"] == "claim":
                present = (e["op"], e["scope"]) in landed
                if e.get("ok", True) and not present:
                    return _OpMessage(
                        f"the agent told the user {e['op']} happened for {_where(e['scope'])}, "
                        f"but no such effect had landed",
                        e["op"],
                    )
                if not e.get("ok", True) and present:
                    return _OpMessage(
                        f"the agent told the user {e['op']} did not happen for "
                        f"{_where(e['scope'])}, but it had already landed",
                        e["op"],
                    )
        return None
    return Invariant("claims_true", check)


def scenario_terminates() -> Invariant:
    def check(world: World) -> str | None:
        if world.completed:
            return None
        errors = [e for e in world.entries if e["kind"] == "error"]
        last = f"; last error: {errors[-1]['text']}" if errors else ""
        return (f"the scenario did not finish within max_restarts={world.max_restarts} "
                f"({world.incarnations} incarnations){last}")
    return Invariant("scenario_terminates", check)
