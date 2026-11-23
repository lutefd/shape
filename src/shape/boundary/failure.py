"""A minimized failure, the distinct root causes a sweep's failing seeds fall into, and how they
are printed."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .faults import FAULTS
from .log import event_order


def kind(key: str, value: int) -> str:
    """What deviation a draw is, without where it happened: `payments.charge:timeout`,
    `model:after_refund=1` (option 1), or `duplicate_trigger`."""
    family, _, rest = key.partition(":")
    subject = rest.split("#")[0]
    if family == "fault":
        return f"{subject}:{FAULTS[value - 1]}"
    if family == "sched":
        return f"{key}={value}"
    return f"model:{subject}={value}" if family == "model" else subject


def kinds(entries: list[dict]) -> tuple[str, ...]:
    """The deviations of a run, in the order they happened."""
    draws = [e for e in entries if e["kind"] in ("fault", "choice", "schedule")]
    draws.sort(key=event_order)
    return tuple(kind(d["key"], d["value"]) for d in draws)


def raw_kinds(realized: dict[str, int]) -> Counter[str]:
    return Counter(kind(k, v) for k, v in realized.items())


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
    signature: tuple = ()  # invariant + ordered deviation kinds of the (minimized) run

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
    """Failing seeds whose minimized runs have the same invariant and the same deviations."""

    failure: Failure  # the seed with the fewest deviations among them
    seeds: list[int] = field(default_factory=list)

    @property
    def deviations(self) -> Counter[str]:
        return Counter(self.failure.signature[1:])


@dataclass
class Report:
    failing: int
    causes: list[Cause]
    skipped: int = 0  # failing seeds beyond the investigation limit

    def render(self) -> str:
        n = len(self.causes)
        word = "cause" if n == 1 else "causes"
        out = [f"{n} distinct {word} across {self.failing} failing seeds"]
        if self.skipped:
            out.append(f"({self.skipped} more failing seeds were not minimized; the ones with the "
                       f"fewest deviations were)")
        for i, cause in enumerate(self.causes, 1):
            ordered = sorted(cause.seeds)
            seen = ", ".join(map(str, ordered[:5])) + (", ..." if len(ordered) > 5 else "")
            heading = (f"cause {i} of {n}: {len(cause.seeds)} of {self.failing} failing seeds "
                       f"({seen})")
            out += ["", heading, cause.failure.render()]
        return "\n".join(out)
