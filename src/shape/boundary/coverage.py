"""What a sweep actually exercised, so a pass that tested nothing cannot pass for a pass."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import Any

from .faults import AFTER_COMMIT

SCENARIO_OP = "(scenario)"  # the op that scenario-level faults (duplicate_trigger) are filed under


def _format_scope_derivation(source: str, rule: str) -> str:
    if source == "explicit":
        return f"explicit ({rule})" if rule else "explicit scope="
    if source == "fake_default":
        return f"fake default rule `{rule}`" if rule else "fake default rule"
    return "effect default"


def of_run(world: Any, draws: list[dict]) -> dict:
    """One seed's counts: effects landed and attempts per op, faults per op and kind, model
    choices taken, and how each op's scope was derived. Plain dicts, so a parallel worker can
    send them back as JSON."""
    faults: dict[str, Counter[str]] = defaultdict(Counter)
    choices: Counter[str] = Counter()
    schedules: Counter[str] = Counter()
    for d in draws:
        if "schedule" in d:
            schedules[f"{d['schedule']}: {d['chosen']} before {d['nominal']}"] += 1
        elif "choice" in d:
            choices[f"{d['choice']} -> {d['chosen']}"] += 1
        elif d["fault"] not in AFTER_COMMIT:
            faults[d["op"]][d["fault"]] += 1
    for e in world.entries:
        if e["kind"] == "effect" and e.get("fault") in AFTER_COMMIT:
            faults[e["op"]][e["fault"]] += 1
    no_effects = Counter(e["op"] for e in world.entries if e["kind"] == "no_effect")
    harness_errors = Counter(e["text"] for e in world.entries if e["kind"] == "harness_error")
    scopes: dict[str, dict[tuple[str, str], str]] = defaultdict(dict)
    for e in (*world.effects(), *world.attempts()):
        source = e.get("scope_source")
        if source is None:
            continue
        rule = e.get("scope_rule") or ""
        scopes[e["op"]].setdefault((source, rule), e["scope"])
    return {
        "effects": dict(Counter(e["op"] for e in world.effects())),
        "attempts": dict(Counter(e["op"] for e in world.attempts())),
        "no_effects": dict(no_effects),
        "harness_errors": dict(harness_errors),
        "faults": dict(faults),
        "choices": dict(choices),
        "schedules": dict(schedules),
        "scopes": {
            op: [[src, rule, ex] for (src, rule), ex in sorted(items.items())]
            for op, items in scopes.items()
        },
    }


class Coverage:
    def __init__(self) -> None:
        self.seeds = 0
        self.effects: Counter[str] = Counter()
        self.attempts: Counter[str] = Counter()
        self.no_effects: Counter[str] = Counter()
        self.harness_errors: Counter[str] = Counter()
        self.faults: dict[str, Counter[str]] = defaultdict(Counter)
        self.choices: Counter[str] = Counter()
        self.schedules: Counter[str] = Counter()
        self.scopes: dict[str, dict[tuple[str, str], str]] = defaultdict(dict)

    def add(self, stats: dict) -> None:
        self.seeds += 1
        self.effects.update(stats["effects"])
        self.attempts.update(stats["attempts"])
        self.no_effects.update(stats.get("no_effects", {}))
        self.harness_errors.update(stats.get("harness_errors", {}))
        self.choices.update(stats["choices"])
        self.schedules.update(stats["schedules"])
        for op, by_kind in stats["faults"].items():
            self.faults[op].update(by_kind)
        for op, items in stats.get("scopes", {}).items():
            for source, rule, example in items:
                self.scopes[op].setdefault((source, rule), example)

    @property
    def calls(self) -> int:
        return sum(self.attempts.values())

    @property
    def landed(self) -> int:
        return sum(self.effects.values())

    def render(self) -> str:
        ops = sorted({*self.effects, *self.attempts, *self.faults})
        width = max([len(op) for op in ops] + [2])
        out = [f"coverage over {self.seeds} seeds:",
               f"  {'op':<{width}}  {'landed':>7}  {'attempts':>8}"]
        for op in ops:
            out.append(f"  {op:<{width}}  {self.effects[op]:>7}  {self.attempts[op]:>8}")
            if faults := ", ".join(f"{k} {n}" for k, n in sorted(self.faults.get(op, {}).items())):
                out.append(f"  {'':<{width}}  faults injected: {faults}")
        if not ops:
            out.append("  no effect was called")
        taken = ", ".join(f"{k} x{n}" for k, n in sorted(self.choices.items()))
        out.append(f"  model choices taken: {taken or 'none'}")
        taken = ", ".join(f"{k} x{n}" for k, n in sorted(self.schedules.items()))
        out.append(f"  interleaving choices taken: {taken or 'none'}")
        return "\n".join(out)

    def render_scopes(self, invariants: Sequence[Any] = ()) -> str:
        if not self.scopes:
            return ""
        dup_by_op: dict[str, list[str]] = defaultdict(list)
        for inv in invariants:
            if getattr(inv, "duplicate_sensitive", False):
                for op in inv.ops:
                    if inv.name not in dup_by_op[op]:
                        dup_by_op[op].append(inv.name)
        rows: list[str] = []
        for op in sorted(self.scopes):
            items = sorted(self.scopes[op].items())
            if self.effects[op] == 0 and self.no_effects[op] > 0:
                items = [item for item in items if item[0][0] != "effect_default"]
            if not items:
                continue
            uses_default = False
            for (source, rule), example in items:
                if source in ("fake_default", "effect_default"):
                    uses_default = True
                desc = _format_scope_derivation(source, rule)
                rows.append(f"  {op}: {desc} (e.g. {example!r})")
            if uses_default and dup_by_op.get(op):
                invs = ", ".join(dup_by_op[op])
                rows.append(
                    f"  warning: {op} uses a default scope rule with {invs}; "
                    f"pass scope= to the fake or effect (or run with --strict-scopes)"
                )
        return "\n".join(["scope assignment:", *rows]) if rows else ""


def never_ran(coverage: Coverage, ops: tuple[str, ...]) -> list[str]:
    """The watched ops no seed ever called."""
    return [op for op in dict.fromkeys(ops) if not coverage.attempts[op]]
