"""The user-facing API: `effect`, `approval`, `scenario`, `Run`, and the per-incarnation context
that every wrapped call goes through."""

from __future__ import annotations

import functools
import inspect
import os
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..choices import ChoiceSource
from .faults import (
    AFTER_COMMIT,
    CRASH_EXIT,
    FAULTS,
    NOMINAL_WEIGHT,
    make_error,
)
from .log import REALIZED, WORLD, JsonlWriter, args_hash


@dataclass(frozen=True)
class Run:
    """What a scenario gets. `dir` survives crashes, so keep your own persistence in it."""

    dir: Path
    restarts: int
    seed: int | None = None


@dataclass(frozen=True)
class EffectSpec:
    op: str
    scope: Callable[..., str] | None
    key: Callable[..., str | None] | None
    irreversible: bool
    errors: Mapping[str, Callable[[], Exception]]


@dataclass
class Scenario:
    fn: Callable[[Run], None]
    name: str
    invariants: Sequence[Any]
    max_restarts: int
    file: str
    module: str

    def __call__(self, run: Run) -> None:
        self.fn(run)


def scenario(fn: Callable[[Run], None] | None = None, *, invariants: Sequence[Any] = (),
             max_restarts: int = 3) -> Any:
    def wrap(f: Callable[[Run], None]) -> Scenario:
        return Scenario(f, f.__name__, tuple(invariants), max_restarts,
                        inspect.getsourcefile(f) or "", f.__module__)
    return wrap(fn) if fn is not None else wrap


def _named_args(sig: inspect.Signature, args: tuple, kwargs: dict) -> dict[str, Any] | None:
    try:
        bound = sig.bind(*args, **kwargs)
    except TypeError:
        return None  # the call is malformed; let the real function raise its own error
    bound.apply_defaults()
    named: dict[str, Any] = {}
    for name, value in bound.arguments.items():
        if sig.parameters[name].kind is inspect.Parameter.VAR_KEYWORD:
            named.update(value)
        else:
            named[name] = value
    return named


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


class _Context:
    """One incarnation's view of the run. Counters, draws and log appends share one lock, so a
    framework that runs sync tools in a thread pool still gets stable per-op keys."""

    def __init__(self, workdir: Path, incarnation: int, choices: ChoiceSource,
                 quiet: bool = False) -> None:
        self.incarnation = incarnation
        self.quiet = quiet  # faults have stopped: the recovery incarnation draws nothing
        self.choices = choices
        self.lock = threading.Lock()
        self.counts: Counter[str] = Counter()
        self.seq = 0
        self.world = JsonlWriter(workdir / WORLD)
        self.realized = JsonlWriter(workdir / REALIZED)
        self.weights = [NOMINAL_WEIGHT] + [1.0 if choices.enabled(f) else 0.0 for f in FAULTS]

    def append(self, writer: JsonlWriter, entry: dict) -> None:
        with self.lock:
            self.seq += 1
            writer.write({**entry, "inc": self.incarnation, "seq": self.seq})

    def call(self, spec: EffectSpec, fn: Callable, sig: inspect.Signature,
             args: tuple, kwargs: dict) -> Any:
        named = _named_args(sig, args, kwargs)
        if named is None:
            return fn(*args, **kwargs)
        shown = {k: v for k, v in named.items() if k not in ("self", "cls")}
        label = fn.__qualname__.rsplit("<locals>.", 1)[-1]
        call = f"{label}({', '.join(f'{k}={_short(v)}' for k, v in shown.items())})"
        scope = spec.scope(**named) if spec.scope else ""
        key = spec.key(**named) if spec.key else None
        with self.lock:
            n = self.counts[spec.op]
            self.counts[spec.op] += 1
            draw_key = f"fault:{spec.op}#{self.incarnation}.{n}"
            value = 0 if self.quiet else self.choices.draw(draw_key, self.weights)
            fault = FAULTS[value - 1] if value else None
            error = make_error(fault, spec.errors) if fault and "crash" not in fault else None
            if fault:
                self.seq += 1
                self.realized.write({
                    "key": draw_key, "value": value, "fault": fault, "op": spec.op, "call": call,
                    "raised": type(error).__name__ if error else None,
                    "inc": self.incarnation, "seq": self.seq})
        if fault == "crash_before":
            os._exit(CRASH_EXIT)
        if error is not None and fault not in AFTER_COMMIT:
            raise error
        result = fn(*args, **kwargs)
        self.append(self.world, {
            "kind": "effect", "op": spec.op, "scope": scope, "key": key, "call": call,
            "irreversible": spec.irreversible, "args_hash": args_hash(shown),
            "fault": fault, "raised": type(error).__name__ if error else None})
        if fault == "crash_after":
            os._exit(CRASH_EXIT)
        if error is not None:
            raise error
        return result


_active: _Context | None = None


def activate(ctx: _Context | None) -> None:
    global _active
    _active = ctx


def effect(op: str, *, scope: Callable[..., str] | None = None,
           key: Callable[..., str | None] | None = None, irreversible: bool = False,
           errors: Mapping[str, Callable[[], Exception]] | None = None) -> Callable:
    """Mark a side-effecting function. Outside a shape run it is the plain function.

    `scope` names the business thing the effect applies to. `key` returns the idempotency key
    the call carries: effects with the same op, scope and key are one effect, because the
    provider dedupes them. `errors` maps a fault name to a zero-argument exception factory.
    """
    spec = EffectSpec(op, scope, key, irreversible, dict(errors or {}))

    def wrap(fn: Callable) -> Callable:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = _active
            if ctx is None:
                return fn(*args, **kwargs)
            return ctx.call(spec, fn, sig, args, kwargs)
        return wrapper
    return wrap


def approval(scope: str, args: Mapping[str, Any] | None = None) -> None:
    """Record that a human (or policy) approved `scope` for exactly these effect arguments.

    Call it from the code that obtains the approval. `args` is the dict of named arguments of
    the effect being approved (without `self`)."""
    ctx = _active
    if ctx is not None:
        ctx.append(ctx.world, {"kind": "approval", "scope": scope,
                               "args_hash": args_hash(dict(args or {}))})
