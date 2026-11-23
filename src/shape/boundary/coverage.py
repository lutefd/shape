"""What a sweep actually exercised, so a pass that tested nothing cannot pass for a pass."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

SCENARIO_OP = "(scenario)"  # the op that scenario-level faults (duplicate_trigger) are filed under


def of_run(world: Any, draws: list[dict]) -> dict:
    """One seed's counts: effects landed and attempts per op, faults per op and kind, and the
    model choices taken. Plain dicts, so a parallel worker can send them back as JSON."""
    faults: dict[str, dict[str, int]] = defaultdict(dict)
    choices: Counter[str] = Counter()
    for d in draws:
        if "choice" in d:
            choices[f"{d['choice']} -> {d['chosen']}"] += 1
        else:
            by_kind = faults[d["op"]]
            by_kind[d["fault"]] = by_kind.get(d["fault"], 0) + 1
    return {"effects": dict(Counter(e["op"] for e in world.effects())),
            "attempts": dict(Counter(e["op"] for e in world.attempts())),
            "faults": dict(faults), "choices": dict(choices)}


class Coverage:
    def __init__(self) -> None:
        self.seeds = 0
        self.effects: Counter[str] = Counter()
        self.attempts: Counter[str] = Counter()
        self.faults: dict[str, Counter[str]] = defaultdict(Counter)
        self.choices: Counter[str] = Counter()

    def add(self, stats: dict) -> None:
        self.seeds += 1
        self.effects.update(stats["effects"])
        self.attempts.update(stats["attempts"])
        self.choices.update(stats["choices"])
        for op, by_kind in stats["faults"].items():
            self.faults[op].update(by_kind)

    @property
    def calls(self) -> int:
        return sum(self.attempts.values())

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
        return "\n".join(out)


def never_ran(coverage: Coverage, ops: tuple[str, ...]) -> list[str]:
    """The watched ops no seed ever called."""
    return [op for op in dict.fromkeys(ops) if not coverage.attempts[op]]
