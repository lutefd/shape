"""A minimized failure, the distinct root causes a sweep's failing seeds fall into, and how they
are printed."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .faults import AFTER_COMMIT, FAULTS
from .log import event_order

_COARSE_TEXT = {
    "reply_lost": ("reply lost", "commit landed, caller saw an error"),
    "crash_after": ("crash after commit", "process died after the commit"),
    "before_commit": ("before-commit fault", "the call failed cleanly"),
    "crash_before": ("crash", "died before the call"),
}


def fault_class(fault_name: str) -> str:
    """Coarse class of a boundary fault: reply_lost (timeout/reset after commit, same process
    retries), crash_after (process died after the commit, recovery after restart), before_commit
    (call failed cleanly), or crash_before (died before the call)."""
    if fault_name == "crash_after":
        return "crash_after"
    if fault_name in AFTER_COMMIT:
        return "reply_lost"
    if fault_name == "crash_before":
        return "crash_before"
    return "before_commit"


def kind(key: str, value: int) -> str:
    """What exact deviation a draw is, without where it happened: `payments.charge:timeout`,
    `model:after_refund=1` (option 1), or `duplicate_trigger`."""
    family, _, rest = key.partition(":")
    subject = rest.split("#")[0]
    if family == "fault":
        return f"{subject}:{FAULTS[value - 1]}"
    if family == "sched":
        return f"{key}={value}"
    return f"model:{subject}={value}" if family == "model" else subject


def coarse_kind(key: str, value: int) -> str:
    """Coarse class of a deviation for root-cause grouping: `<op>:reply_lost`, `<op>:crash_after`,
    `<op>:before_commit`, `<op>:crash_before`, `interleaving`, `model:<label>=<value>`, or
    `duplicate_trigger`."""
    family, _, rest = key.partition(":")
    subject = rest.split("#")[0]
    if family == "fault":
        return f"{subject}:{fault_class(FAULTS[value - 1])}"
    if family == "sched":
        return "interleaving"
    return f"model:{subject}={value}" if family == "model" else subject


def kinds(entries: list[dict]) -> tuple[str, ...]:
    """The exact deviations of a run, in the order they happened."""
    draws = [e for e in entries if e["kind"] in ("fault", "choice", "schedule")]
    draws.sort(key=event_order)
    return tuple(kind(d["key"], d["value"]) for d in draws)


def coarse_kinds(entries: list[dict]) -> tuple[str, ...]:
    """The coarse deviation classes of a run, in the order they happened."""
    draws = [e for e in entries if e["kind"] in ("fault", "choice", "schedule")]
    draws.sort(key=event_order)
    return tuple(coarse_kind(d["key"], d["value"]) for d in draws)


def raw_kinds(realized: dict[str, int]) -> Counter[str]:
    return Counter(kind(k, v) for k, v in realized.items())


def raw_coarse_kinds(realized: dict[str, int]) -> Counter[str]:
    return Counter(coarse_kind(k, v) for k, v in realized.items())


def _deviation_category(coarse_dev: str) -> str:
    if coarse_dev == "interleaving":
        return "interleaving"
    if coarse_dev.startswith("model:"):
        return "model"
    if coarse_dev == "duplicate_trigger":
        return "duplicate_trigger"
    _, _, cls = coarse_dev.rpartition(":")
    return cls if cls in _COARSE_TEXT else coarse_dev


def _exact_fault_name(exact_dev: str) -> tuple[str, str] | None:
    if exact_dev.startswith("model:") or ":" not in exact_dev:
        return None
    op, _, fault_name = exact_dev.rpartition(":")
    return (op, fault_name) if fault_name in FAULTS else None


@dataclass
class Failure:
    scenario: str
    target: str
    seed: int
    invariant: str
    message: str
    trace: list[str]
    steps: int
    deviations: int
    original_deviations: int
    replays: int
    replay_file: Path | None
    problem: str | None = None  # set when the run does not reproduce: no minimality claim then
    signature: tuple = ()  # invariant (+ op if distinct) + ordered coarse deviations
    op: str | None = None
    coarse_deviations: tuple[str, ...] = ()
    exact_kinds: tuple[str, ...] = ()

    def render(self) -> str:
        head = f"FAIL {self.target}  seed={self.seed}  invariant={self.invariant}"
        out = [head, f"  {self.message}"]
        if self.problem:
            out += [f"  NOT MINIMIZED: {self.problem}",
                    f"  original run ({self.original_deviations} deviations) as it happened:"]
        else:
            out.append(f"  minimized to {self.steps} steps ({self.deviations} of "
                       f"{self.original_deviations} deviations kept, {self.replays} replays):")
        out += [f"    {line}" for line in self.trace]
        if self.replay_file:
            out.append(f"  replay: {self.replay_file}")
        return "\n".join(out)


@dataclass
class Cause:
    """Failing seeds whose minimized runs share the same invariant, violating op, and coarse
    deviation classes, with the exact fault kinds observed listed inside the cause."""

    failure: Failure  # the seed with the fewest deviations among them
    seeds: list[int] = field(default_factory=list)
    exact_kinds: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.exact_kinds.update(self.failure.exact_kinds)

    @property
    def deviations(self) -> Counter[str]:
        return Counter(self.failure.coarse_deviations)

    @property
    def is_nominal(self) -> bool:
        return not self.failure.coarse_deviations

    def record_exact(self, realized: dict[str, int]) -> None:
        wanted = self.deviations
        for k, v in realized.items():
            if coarse_kind(k, v) in wanted:
                self.exact_kinds.add(kind(k, v))

    def trigger_label(self) -> str:
        if self.failure.invariant == "harness_error":
            return "harness error (a shape hook raised; fix the harness)"
        if self.is_nominal:
            return "nominal path (0 faults, 0 model choices): a plain unit test catches this"
        by_class: dict[str, set[str]] = {}
        for dev in self.exact_kinds:
            parsed = _exact_fault_name(dev)
            if parsed is not None:
                _, fname = parsed
                by_class.setdefault(fault_class(fname), set()).add(fname)
        seen_cats: list[str] = []
        for cdev in self.failure.coarse_deviations:
            cat = _deviation_category(cdev)
            if cat not in seen_cats:
                seen_cats.append(cat)
        parts: list[str] = []
        for cat in seen_cats:
            if cat in _COARSE_TEXT:
                short, desc = _COARSE_TEXT[cat]
                names = sorted(by_class.get(cat, ()))
                detail = f"{desc}: {', '.join(names)}" if names else desc
                parts.append(f"{short} ({detail})")
            elif cat == "model":
                parts.append("model choice")
            elif cat == "duplicate_trigger":
                parts.append("duplicate delivery")
            else:
                parts.append(cat)
        return "needs: " + " + ".join(parts)


@dataclass
class Report:
    failing: int
    causes: list[Cause]
    skipped: int = 0  # failing seeds beyond the investigation limit
    budget_skipped: int = 0

    def render(self) -> str:
        n = len(self.causes)
        word = "cause" if n == 1 else "causes"
        out = [f"{n} distinct {word} across {self.failing} failing seeds"]
        if self.skipped:
            out.append(f"({self.skipped} more failing seeds were not minimized due to max_shrinks)")
        if self.budget_skipped:
            out.append(f"({self.budget_skipped} more failing seeds were not minimized "
                       "because the time budget expired)")
        for i, cause in enumerate(self.causes, 1):
            ordered = sorted(cause.seeds)
            seen = ", ".join(map(str, ordered[:5])) + (", ..." if len(ordered) > 5 else "")
            heading = (f"cause {i} of {n}: {len(cause.seeds)} of {self.failing} failing seeds "
                       f"({seen})")
            out += ["", heading, f"  {cause.trigger_label()}", cause.failure.render()]
        return "\n".join(out)
