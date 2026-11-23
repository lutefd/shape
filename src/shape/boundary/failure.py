"""A sweep's defects, the triggers that reproduced each one, and their replay artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .faults import AFTER_COMMIT, DUPLICATE_TRIGGER
from .invariants import Violation
from .log import event_order

_TRIGGER_LABELS = {
    "reply_lost": "reply lost",
    "crash_after": "crash after commit",
    "crash_before": "crash before",
    "before_commit": "before-commit fault",
    "model": "model choice",
    "interleaving": "interleaving",
    "duplicate_trigger": "duplicate trigger",
    "nominal": "nominal (a plain unit test catches this)",
}

# Keep field names in scopes such as tenant:acme|order:123 or tenant:acme/order:123.
_SCOPE_FIELD = re.compile(r"(?:^|[|/])[A-Za-z_][\w.-]*:")


def scope_pattern(scope: str | None) -> str | None:
    if not scope:
        return scope
    fields = list(_SCOPE_FIELD.finditer(scope))
    if not fields:
        return "<id>"
    prefix = "<id>" if fields[0].start() else ""
    return prefix + "".join(f"{m[0]}<id>" for m in fields)


@dataclass(frozen=True)
class DefectKey:
    invariant: str
    op: str | None
    scope_pattern: str | None

    @classmethod
    def from_violation(cls, violation: Violation) -> DefectKey:
        return cls(violation.invariant, violation.op, scope_pattern(violation.scope))


def fault_class(fault_name: str) -> str:
    if fault_name in ("crash_before", "crash_after"):
        return fault_name
    return "reply_lost" if fault_name in AFTER_COMMIT else "before_commit"


def trigger_classes(entries: list[dict]) -> dict[str, set[str]]:
    classes: dict[str, set[str]] = {}
    for event in entries:
        if event["kind"] == "choice":
            classes.setdefault("model", set())
        elif event["kind"] == "schedule":
            classes.setdefault("interleaving", set())
        elif event["kind"] == "fault":
            fault = event["fault"]
            if fault == DUPLICATE_TRIGGER:
                classes.setdefault("duplicate_trigger", set())
            else:
                classes.setdefault(fault_class(fault), set()).add(fault)
    return classes or {"nominal": set()}


def _deviation_order(item: tuple[int, dict]) -> tuple[int, int, int, int]:
    idx, event = item
    if "inc" in event and "seq" in event:
        inc, turn, seq = event_order(event)
        return inc, turn, seq, idx
    return 0, 0, idx, idx


def _format_deviation(event: dict) -> str:
    kind = event["kind"]
    if kind == "choice":
        return _TRIGGER_LABELS["model"]
    if kind == "schedule":
        return _TRIGGER_LABELS["interleaving"]
    fault = event["fault"]
    if fault == DUPLICATE_TRIGGER:
        return _TRIGGER_LABELS["duplicate_trigger"]
    cls = fault_class(fault)
    label = _TRIGGER_LABELS[cls]
    if cls in ("crash_after", "crash_before"):
        return label
    raised = event.get("raised") or ""
    if isinstance(raised, str) and raised.startswith("HTTP "):
        detail = raised.removeprefix("HTTP ")
    else:
        detail = fault
    return f"{label} ({detail})"


def describe_triggers(entries: list[dict]) -> str:
    """Describe every kept deviation in a minimized trace, in execution order."""
    events = [
        (idx, event)
        for idx, event in enumerate(entries)
        if event.get("kind") in ("fault", "choice", "schedule")
    ]
    if not events:
        return _TRIGGER_LABELS["nominal"]
    return " + ".join(_format_deviation(e) for _, e in sorted(events, key=_deviation_order))


@dataclass
class Failure:
    target: str
    seed: int
    key: DefectKey
    message: str
    trace: list[str]
    steps: int
    deviations: int
    original_deviations: int
    replays: int
    replay_file: Path
    problem: str | None = None
    triggers: dict[str, set[str]] = field(default_factory=dict)
    trigger_label: str = ""

    @property
    def size(self) -> tuple[bool, int, int, int, int]:
        return self.problem is not None, self.deviations, len(self.triggers), self.steps, self.seed

    def render(self) -> str:
        head = f"FAIL {self.target}  seed={self.seed}  invariant={self.key.invariant}"
        out = [head, f"  {self.message}"]
        if self.problem:
            out += [f"  NOT MINIMIZED: {self.problem}",
                    f"  original run ({self.original_deviations} deviations) as it happened:"]
        else:
            if self.trigger_label:
                out.append(f"  trigger: {self.trigger_label}")
            out.append(f"  minimized to {self.steps} steps ({self.deviations} of "
                       f"{self.original_deviations} deviations kept, {self.replays} replays):")
        out += [f"    {line}" for line in self.trace]
        if self.problem is None:
            out.append(f"  replay: {self.replay_file}")
        return "\n".join(out)


@dataclass
class Trigger:
    name: str
    failure: Failure
    seeds: list[int] = field(default_factory=list)
    faults: set[str] = field(default_factory=set)

    def record(self, failure: Failure) -> None:
        if failure.seed not in self.seeds:
            self.seeds.append(failure.seed)
        self.faults.update(failure.triggers[self.name])
        self.failure = min(self.failure, failure, key=lambda f: f.size)

    def record_confirmed(self, seed: int, faults: set[str] | tuple[str, ...] = ()) -> None:
        if seed not in self.seeds:
            self.seeds.append(seed)
        self.faults.update(faults)

    @property
    def label(self) -> str:
        return _TRIGGER_LABELS[self.name]

    def as_dict(self) -> dict:
        return {
            "class": self.name,
            "label": self.label,
            "seeds": sorted(self.seeds),
            "faults": sorted(self.faults),
            "representative_seed": self.failure.seed,
            "replay_path": str(self.failure.replay_file),
            "trigger_label": self.failure.trigger_label,
        }


@dataclass
class Defect:
    key: DefectKey
    seeds: list[int] = field(default_factory=list)
    triggers: dict[str, Trigger] = field(default_factory=dict)
    failure: Failure | None = None

    def record(self, failure: Failure) -> None:
        if self.failure is None or failure.size < self.failure.size:
            self.failure = failure
        for name in failure.triggers:
            self.triggers.setdefault(name, Trigger(name, failure)).record(failure)

    def record_confirmed(self, seed: int, raw_triggers: dict[str, set[str]]) -> None:
        matched = False
        for name, faults in raw_triggers.items():
            if name in self.triggers:
                self.triggers[name].record_confirmed(seed, faults)
                matched = True
        if not matched and "nominal" in self.triggers:
            self.triggers["nominal"].record_confirmed(seed)

    def has_nominal_and_faults(self) -> bool:
        return "nominal" in self.triggers and len(self.triggers) > 1

    def fault_driven_seed_count(self) -> int:
        return len({s for name, t in self.triggers.items() if name != "nominal" for s in t.seeds})

    def ranked_triggers(self) -> list[Trigger]:
        return sorted(self.triggers.values(), key=lambda t: (t.name == "nominal", -len(t.seeds),
                                                             t.name))

    def as_dict(self) -> dict:
        failure = self.failure
        return {
            "invariant": self.key.invariant,
            "op": self.key.op,
            "scope_pattern": self.key.scope_pattern,
            "seeds": sorted(self.seeds),
            "representative_seed": failure.seed if failure else None,
            "replay_path": str(failure.replay_file) if failure and not failure.problem else None,
            "trigger_label": failure.trigger_label if failure and not failure.problem else None,
            "message": failure.message if failure else None,
            "triggers": [t.as_dict() for t in self.ranked_triggers()],
        }


@dataclass
class Report:
    failing: int
    defects: list[Defect]
    skipped_seeds: list[int] = field(default_factory=list)
    budget_skipped_seeds: list[int] = field(default_factory=list)
    unreproducible: list[Failure] = field(default_factory=list)

    @property
    def skipped(self) -> int:
        return len(self.skipped_seeds)

    @property
    def budget_skipped(self) -> int:
        return len(self.budget_skipped_seeds)

    def render(self, *, all_triggers: bool = False) -> str:
        n = len(self.defects)
        out = [f"{n} {'defect' if n == 1 else 'defects'} across {self.failing} failing seeds"]
        if self.skipped:
            out.append(f"({self.skipped} more failing seeds were not minimized due to max_shrinks)")
        if self.budget_skipped:
            out.append(f"({self.budget_skipped} more failing seeds were not minimized "
                       "because the time budget expired)")
        for i, defect in enumerate(self.defects, 1):
            key = defect.key
            count = len(defect.seeds)
            scope = "(run)" if key.scope_pattern == "" else key.scope_pattern or "-"
            heading = (f"defect {i} of {n}: {key.invariant}  op={key.op or '-'}  "
                       f"scope={scope}  ({count} of {self.failing} failing seeds)")
            out.extend(["", heading])
            triggers = defect.ranked_triggers()
            for trigger in triggers:
                count = len(trigger.seeds)
                faults = f" ({', '.join(sorted(trigger.faults))})" if trigger.faults else ""
                out.append(f"  {trigger.label}: {count} {'seed' if count == 1 else 'seeds'}{faults}")
            if defect.failure is not None:
                out.append(defect.failure.render())
                nominal_and_faults = defect.has_nominal_and_faults()
                if nominal_and_faults:
                    n_fault = defect.fault_driven_seed_count()
                    clause = "seed also fails" if n_fault == 1 else "seeds also fail"
                    out.append(
                        f"  nominal path fails; {n_fault} fault-driven {clause}; "
                        "their minimized traces differ:"
                    )
                if all_triggers or nominal_and_faults:
                    shown = {defect.failure.replay_file}
                    for trigger in triggers:
                        if trigger.failure.replay_file in shown:
                            if all_triggers:
                                out.append(f"  {trigger.label} replay: shown above")
                        else:
                            out.extend([f"  {trigger.label} replay:", trigger.failure.render()])
                            shown.add(trigger.failure.replay_file)
        for failed in self.unreproducible:
            if all(defect.failure is not failed for defect in self.defects):
                out.extend(["", failed.render()])
        return "\n".join(out)

    def as_dict(self) -> dict:
        return {
            "defects": [defect.as_dict() for defect in self.defects],
            "shrink_limit_skipped_seeds": sorted(self.skipped_seeds),
            "shrink_budget_skipped_seeds": sorted(self.budget_skipped_seeds),
            "unreproducible": [
                {"seed": f.seed, "invariant": f.key.invariant, "reason": f.problem}
                for f in self.unreproducible
            ],
        }
