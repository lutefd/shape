"""Shared effect specs and plan perturbation engine for framework adapters.

Zero framework dependencies: converts a plain plan (tool calls + final message) into deterministic,
shrinkable `shape.choose` decisions that explore model misbehaviors (repeat, drop, swap, argument
substitution, error retry count, and post-restart re-issue).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import Any

from ..boundary import api

_RETRY_OPTIONS = ("retry_1", "retry_0", "retry_2")
_RETRY_BUDGET = {"retry_0": 0, "retry_1": 1, "retry_2": 2}


@dataclass(frozen=True)
class Effect:
    """Side-effect specification for one tool in `effects(tools, ops={...})`."""

    op: str
    scope: Callable[..., str] | None = None
    key: Callable[..., str | None] | None = None
    irreversible: bool = False
    errors: Mapping[str, Callable[[], Exception]] | None = None
    idempotent: bool = False
    result: Callable[..., Any] | None = None
    hide: Sequence[str] = ()
    landed: Callable[..., Any] | None = None
    key_from_result: Callable[..., str | None] | None = None
    delivery: str | None = None


# Every option of `@shape.effect` is a field of `Effect`: unknown keys are rejected against the
# fields, and the decorator is built from them, so a new option is forwarded by adding a field.
_EFFECT_KEYS = frozenset(f.name for f in fields(Effect))


EffectConfig = str | Mapping[str, Any] | Effect


def normalize_effect(tool_name: str, cfg: EffectConfig) -> Effect:
    if isinstance(cfg, Effect):
        return cfg
    if isinstance(cfg, str):
        return Effect(op=cfg)
    if isinstance(cfg, Mapping):
        unknown = set(cfg) - _EFFECT_KEYS
        if unknown:
            raise ValueError(
                f"unknown effect option(s) for tool {tool_name!r}: {sorted(unknown)}"
            )
        if "op" not in cfg:
            raise ValueError(f"effect config for tool {tool_name!r} must include 'op'")
        return Effect(**{
            **cfg,
            "irreversible": bool(cfg.get("irreversible", False)),
            "idempotent": bool(cfg.get("idempotent", False)),
            "hide": tuple(cfg.get("hide", ())),
        })
    raise TypeError(
        f"ops[{tool_name!r}] must be an op name, dict, or Effect; got {type(cfg).__name__}"
    )


def make_effect_decorator(spec: Effect) -> Callable:
    return api.effect(spec.op, **{f.name: getattr(spec, f.name)
                                  for f in fields(spec) if f.name != "op"})


def compose_landed(
    is_framework_error: Callable[[Any], bool],
    user_landed: Callable[..., Any] | None,
) -> Callable[..., Any]:
    """Treat framework-converted error results as not landed, then apply any user `landed=`."""
    if user_landed is None:
        return lambda value: not is_framework_error(value)

    def _check(value: Any, **kwargs: Any) -> bool:
        if is_framework_error(value):
            return False
        return bool(api._invoke_result_callable(user_landed, value, kwargs))

    _check.__shape_user_fn__ = user_landed  # type: ignore[attr-defined]
    return _check


def _call_text(step: _PendingStep) -> str:
    """The planned call as the trace should name it: `issue_refund(order_id='17')`."""
    shown = ", ".join(f"{k}={v!r}" for k, v in step.args.items())
    return f"{step.name}({shown})"


@dataclass(frozen=True)
class Call:
    """One planned tool call in a scripted model plan."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    alternatives: Mapping[str, Sequence[Any]] | None = None


PlanStep = Call | tuple[str, Mapping[str, Any]] | Mapping[str, Any] | str


@dataclass(frozen=True)
class HistoryCall:
    call_id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class HistoryResult:
    call_id: str
    is_error: bool = False
    is_rejected: bool = False
    content: str = ""


@dataclass(frozen=True)
class HistoryUser:
    pass


HistoryEvent = HistoryUser | HistoryCall | HistoryResult


@dataclass(frozen=True)
class CallAction:
    call_id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class FinalAction:
    text: str


Action = CallAction | FinalAction


@dataclass
class _PendingStep:
    num: int
    name: str
    args: dict[str, Any]
    alternatives: list[tuple[str, Any]]
    perturbed: bool = False
    retry_budget: int | None = None
    retries_used: int = 0

    def clone(self, *, args: dict[str, Any] | None = None, perturbed: bool = True) -> _PendingStep:
        return _PendingStep(
            num=self.num,
            name=self.name,
            args=dict(self.args if args is None else args),
            alternatives=list(self.alternatives),
            perturbed=perturbed,
        )


def _resolve_alternatives(
    name: str,
    args: dict[str, Any],
    step_alts: Mapping[str, Sequence[Any]] | None,
    global_alts: Mapping[str, Any] | None,
) -> list[tuple[str, Any]]:
    merged: dict[str, list[Any]] = {}

    def add(mapping: Mapping[str, Sequence[Any]] | None, *, require_present: bool) -> None:
        if not mapping:
            return
        for arg_name, values in mapping.items():
            if require_present and arg_name not in args:
                continue
            bucket = merged.setdefault(arg_name, [])
            for v in values:
                if args.get(arg_name) != v and v not in bucket:
                    bucket.append(v)

    if global_alts:
        flat: dict[str, Sequence[Any]] = {}
        scoped: Mapping[str, Sequence[Any]] | None = None
        for k, v in global_alts.items():
            if isinstance(v, Mapping):
                if k == name:
                    scoped = v
            else:
                flat[k] = v
        add(flat, require_present=True)
        add(scoped, require_present=False)
    add(step_alts, require_present=False)
    return [(arg_name, value) for arg_name, values in merged.items() for value in values]


def normalize_plan(
    plan: Sequence[PlanStep],
    final: str | None,
    alternatives: Mapping[str, Any] | None,
) -> tuple[list[_PendingStep], str]:
    steps: list[_PendingStep] = []
    inferred_final: str | None = None
    for idx, item in enumerate(plan):
        if isinstance(item, str):
            if idx != len(plan) - 1:
                raise ValueError("a string final message may only appear as the last plan entry")
            inferred_final = item
            continue
        if isinstance(item, Call):
            name, args, step_alts = item.name, dict(item.args), item.alternatives
        elif isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str):
            name, args, step_alts = item[0], dict(item[1]), None
        elif isinstance(item, Mapping) and "name" in item:
            name = str(item["name"])
            args = dict(item.get("args", {}))
            step_alts = item.get("alternatives")
        else:
            raise TypeError(
                "each plan step must be a (tool_name, args) tuple, Call, or {'name', 'args'} dict"
            )
        num = len(steps) + 1
        alts = _resolve_alternatives(name, args, step_alts, alternatives)
        steps.append(_PendingStep(num=num, name=name, args=args, alternatives=alts))
    resolved_final = final if final is not None else (inferred_final or "Done.")
    return steps, resolved_final


def _is_restart(history: Sequence[HistoryEvent]) -> bool:
    ctx = api._active
    if ctx is not None and ctx.incarnation > 0:
        return True
    return sum(1 for e in history if isinstance(e, HistoryUser)) > 1


class PlanRunner:
    """Executes a normalized plan with shrinkable `shape.choose` perturbations.

    Nominal (all choices 0) executes the plan in order and returns `final`.
    """

    def __init__(
        self,
        plan: Sequence[PlanStep],
        *,
        final: str | None = None,
        alternatives: Mapping[str, Any] | None = None,
    ) -> None:
        self._template, self.final = normalize_plan(plan, final, alternatives)
        self._initialized = False
        self._queue: list[_PendingStep] = []
        self._in_flight: _PendingStep | None = None
        self._in_flight_call_id: str | None = None
        self._emitted = 0

    def _next_call_id(self, history: Sequence[HistoryEvent]) -> str:
        seen = {e.call_id for e in history if isinstance(e, HistoryCall)}
        while True:
            self._emitted += 1
            cid = f"call_{self._emitted}"
            if cid not in seen:
                return cid

    def _initialize(self, history: Sequence[HistoryEvent]) -> Action | None:
        self._initialized = True
        calls = [e for e in history if isinstance(e, HistoryCall)]
        results = {e.call_id: e for e in history if isinstance(e, HistoryResult)}

        # If the most recent tool output in resumed history was a rejection, stop immediately.
        last_result = next((e for e in reversed(history) if isinstance(e, HistoryResult)), None)
        if last_result is not None and last_result.is_rejected:
            call_by_id = {c.call_id: c for c in calls}
            rejected_call = call_by_id.get(last_result.call_id)
            label = rejected_call.name if rejected_call else "tool"
            return FinalAction(f"{label} was not approved.")

        remaining = [s.clone(perturbed=False) for s in self._template]
        completed: list[_PendingStep] = []
        for call in calls:
            res = results.get(call.call_id)
            if res is None or res.is_error or res.is_rejected:
                continue
            match_idx = next((i for i, s in enumerate(remaining) if s.name == call.name), None)
            if match_idx is not None:
                completed.append(remaining.pop(match_idx))

        reissued: list[_PendingStep] = []
        if completed and _is_restart(history):
            for step in completed:
                choice = api.choose(
                    f"after restart: call {step.num}: {step.name}",
                    ["nominal", "reissue"],
                    nominal=f"do not repeat {_call_text(step)}",
                )
                if choice == "reissue":
                    reissued.append(step.clone(perturbed=True))

        self._queue = reissued + remaining

        # If history ended with a tool error (e.g. checkpointed after a tool error before crash),
        # treat the matching next remaining step as having just errored.
        if (
            not reissued
            and last_result is not None
            and last_result.is_error
            and history
            and history[-1] is last_result
            and self._queue
        ):
            call_by_id = {c.call_id: c for c in calls}
            failed = call_by_id.get(last_result.call_id)
            if failed is not None and failed.name == self._queue[0].name:
                self._in_flight = self._queue.pop(0)
                self._in_flight_call_id = failed.call_id
        return None

    def _emit(self, step: _PendingStep, history: Sequence[HistoryEvent]) -> CallAction:
        cid = self._next_call_id(history)
        self._in_flight = step
        self._in_flight_call_id = cid
        return CallAction(cid, step.name, dict(step.args))

    def decide(self, history: Sequence[HistoryEvent]) -> Action:
        if not self._initialized:
            early = self._initialize(history)
            if early is not None:
                return early

        results = {e.call_id: e for e in history if isinstance(e, HistoryResult)}
        if self._in_flight is not None and self._in_flight_call_id is not None:
            res = results.get(self._in_flight_call_id)
            if res is not None:
                step = self._in_flight
                if res.is_rejected:
                    self._in_flight = None
                    self._in_flight_call_id = None
                    self._queue.clear()
                    return FinalAction(f"{step.name} was not approved.")
                if res.is_error:
                    if step.retry_budget is None:
                        choice = api.choose(
                            f"error at call {step.num}: {step.name}",
                            _RETRY_OPTIONS,
                        )
                        step.retry_budget = _RETRY_BUDGET[choice]
                    if step.retries_used < step.retry_budget:
                        step.retries_used += 1
                        return self._emit(step, history)
                    self._in_flight = None
                    self._in_flight_call_id = None
                    self._queue.clear()
                    return FinalAction(f"Could not complete {step.name}.")
                # Succeeded.
                self._in_flight = None
                self._in_flight_call_id = None

        while self._queue:
            step = self._queue.pop(0)
            if step.perturbed:
                return self._emit(step, history)

            options = ["nominal", "repeat", "drop"]
            can_swap = bool(self._queue) and not self._queue[0].perturbed
            if can_swap:
                options.append("swap")
            sub_map: dict[str, tuple[str, Any]] = {}
            for arg_name, alt_val in step.alternatives:
                opt = f"substitute {arg_name}={alt_val!r}"
                options.append(opt)
                sub_map[opt] = (arg_name, alt_val)

            choice = api.choose(f"call {step.num}: {step.name}", options,
                                nominal=_call_text(step))
            if choice == "nominal":
                return self._emit(step, history)
            if choice == "repeat":
                self._queue.insert(0, step.clone(perturbed=True))
                return self._emit(step, history)
            if choice == "drop":
                continue
            if choice == "swap":
                nxt = self._queue.pop(0)
                self._queue.insert(0, step.clone(perturbed=True))
                return self._emit(nxt.clone(perturbed=True), history)
            arg_name, alt_val = sub_map[choice]
            new_args = {**step.args, arg_name: alt_val}
            return self._emit(step.clone(args=new_args, perturbed=True), history)

        return FinalAction(self.final)
