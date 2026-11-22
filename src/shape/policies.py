"""Policies: the untrusted agent. `decide(step, observation) -> Action`.

The policy stands in for the model. It sees what the runtime shows it and says what to do
next. `AdversarialPolicy` makes every decision a draw, so the model's mistakes are part of the
run's `realized` map; the others replay or perturb a recorded script.

Draws: `agent#<step>` picks the behavior, `agent#<step>:arg` picks its detail.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .choices import ChoiceSource
from .sim.tools import ToolSpec


@dataclass(frozen=True)
class Action:
    kind: str  # "call" | "abort" | "done"
    tool: str = ""
    op: str = ""
    args: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_json(data: dict[str, Any]) -> Action:
        return Action(**data)

    def render(self) -> str:
        if self.kind != "call":
            return self.kind
        return f"{self.tool}.{self.op}({', '.join(f'{k}={v}' for k, v in self.args.items())})"


@dataclass(frozen=True)
class Observation:
    step: int
    order: dict[str, Any]
    done: tuple[str, ...]  # "tool.op" of calls that have completed
    last: dict[str, Any] | None  # result or {"error": ...} of the previous action


class Policy(Protocol):
    def decide(self, step: int, obs: Observation) -> Action: ...


def plan(order: dict[str, Any]) -> list[Action]:
    """The nominal sequence for a place-order saga."""
    return [
        Action("call", "inventory", "reserve", {"sku": order["sku"], "qty": order["qty"]}),
        Action("call", "payments", "charge", {"order": order["order"], "amount": order["amount"]}),
        Action("call", "email", "send", {"order": order["order"], "to": order["to"]}),
    ]


def nominal(obs: Observation) -> Action:
    for action in plan(obs.order):
        if f"{action.tool}.{action.op}" not in obs.done:
            return action
    return Action("done")


KINDS = ("nominal", "duplicate", "wrong_id", "skip", "call_after_failure", "out_of_order", "abort")
KIND_WEIGHTS = [0.74, 0.05, 0.05, 0.05, 0.04, 0.05, 0.02]


def _fill(spec: ToolSpec, order: dict[str, Any]) -> dict[str, Any]:
    return {k: order.get(k, 1 if t == "int" else "x") for k, t in spec.schema.items()}


class AdversarialPolicy:
    def __init__(self, choices: ChoiceSource, specs: list[ToolSpec]) -> None:
        self.choices, self.specs = choices, specs
        self.history: list[Action] = []

    def decide(self, step: int, obs: Observation) -> Action:
        on = self.choices.enabled("agent")
        weights = KIND_WEIGHTS if on else [1.0] + [0.0] * (len(KINDS) - 1)
        kind = KINDS[self.choices.draw(f"agent#{step}", weights)]
        base = nominal(obs)
        action = base
        if kind == "duplicate" and self.history:
            action = self.history[self._pick(step, len(self.history))]
        elif kind == "wrong_id" and base.kind == "call":
            action = self._wrong_id(step, base)
        elif kind == "skip":
            remaining = [a for a in plan(obs.order) if f"{a.tool}.{a.op}" not in obs.done]
            action = remaining[1] if len(remaining) > 1 else base
        elif kind == "call_after_failure" and self.history:
            action = self.history[-1]
        elif kind == "out_of_order":
            spec = self.specs[self._pick(step, len(self.specs))]
            action = Action("call", spec.tool, spec.op, _fill(spec, obs.order))
        elif kind == "abort":
            action = Action("abort")
        if action.kind == "call":
            self.history.append(action)
        return action

    def _pick(self, step: int, n: int) -> int:
        return self.choices.draw(f"agent#{step}:arg", [1.0] * n)

    def _wrong_id(self, step: int, base: Action) -> Action:
        fields = list(base.args)
        which = self._pick(step, len(fields))
        value = base.args[fields[which]]
        wrong = value + 1 if isinstance(value, int) else f"{value}-x"
        return Action("call", base.tool, base.op, {**base.args, fields[which]: wrong})


class RecordingPolicy:
    """Wraps a policy and records its decisions as a script keyed by step."""

    def __init__(self, inner: Policy) -> None:
        self.inner = inner
        self.script: dict[str, dict[str, Any]] = {}

    def decide(self, step: int, obs: Observation) -> Action:
        action = self.inner.decide(step, obs)
        self.script[str(step)] = asdict(action)
        return action


class ReplayPolicy:
    """Replays a script (the JSON a RecordingPolicy produced). Past its end, the agent is done."""

    def __init__(self, script: dict[str, dict[str, Any]]) -> None:
        self.script = script

    def decide(self, step: int, obs: Observation) -> Action:
        recorded = self.script.get(str(step))
        return Action.from_json(recorded) if recorded else Action("done")


class PerturbedReplay(ReplayPolicy):
    """Replay a script, but at `at_step` let `adversary` (driven by the seed) decide instead."""

    def __init__(self, script: dict[str, dict[str, Any]], at_step: int, adversary: Policy) -> None:
        super().__init__(script)
        self.at_step, self.adversary = at_step, adversary

    def decide(self, step: int, obs: Observation) -> Action:
        if step == self.at_step:
            return self.adversary.decide(step, obs)
        return super().decide(step, obs)
