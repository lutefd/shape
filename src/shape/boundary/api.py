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
from .log import PARENT_SEQ, REALIZED, WORLD, JsonlWriter, args_hash
from .rootcause import mark_injected

# Arguments a framework injects into a tool call. They are not business arguments, so they never
# appear in a rendered call, the world log or an argument hash. `@shape.effect(hide=...)` adds more.
HIDDEN_ARGS = frozenset({"self", "cls", "config", "runtime", "callbacks", "run_manager",
                         "tool_call_id", "context"})


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
    idempotent: bool = False
    result: Callable[[Any], Any] | None = None
    hide: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Actor:
    """A function the parent process calls between incarnations to change the user's world."""

    fn: Callable[[Run], None]
    name: str

    def __call__(self, run: Run) -> None:
        self.fn(run)


@dataclass
class Scenario:
    fn: Callable[[Run], None]
    name: str
    invariants: Sequence[Any]
    max_restarts: int
    file: str
    module: str
    actors: Sequence[Actor] = ()
    duplicate_trigger: bool = False
    expect_effects: bool = True

    def __call__(self, run: Run) -> None:
        self.fn(run)


def scenario(fn: Callable[[Run], None] | None = None, *, invariants: Sequence[Any] = (),
             max_restarts: int = 3, actors: Sequence[Actor] = (),
             duplicate_trigger: bool = False, expect_effects: bool = True) -> Any:
    def wrap(f: Callable[[Run], None]) -> Scenario:
        return Scenario(f, f.__name__, tuple(invariants), max_restarts,
                        inspect.getsourcefile(f) or "", f.__module__, tuple(actors),
                        duplicate_trigger, expect_effects)
    return wrap(fn) if fn is not None else wrap


def actor(fn: Callable[[Run], None]) -> Actor:
    """Mark a function the parent calls after an incarnation dies, before the next one starts.

    It gets the `Run` of the incarnation about to start and may change the user's state (edit a
    file or row, revoke an approval) using `shape.choose`, so a seed decides what the user did
    while the agent was down. It runs in the parent process, with no faults injected."""
    return Actor(fn, fn.__name__)


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


def _result(spec: EffectSpec, value: Any) -> Any:
    """What the effect returned to the world, as `result=` describes it. A `result=` that raises
    must not lose an effect that already landed, so its error is logged in its place."""
    if spec.result is None:
        return None
    try:
        return spec.result(value)
    except Exception as exc:  # noqa: BLE001 - the user's callable; the effect is logged regardless
        return f"<result= raised {type(exc).__name__}: {exc}>"


class _Context:
    """One incarnation's view of the run. Counters, draws and log appends share one lock, so a
    framework that runs sync tools in a thread pool still gets stable per-op keys."""

    def __init__(self, workdir: Path, incarnation: int, choices: ChoiceSource,
                 quiet: bool = False, twin: int = 0, actors: bool = False) -> None:
        self.incarnation = incarnation
        self.twin = twin  # 1 for the duplicate delivery of incarnation 0
        self.actors = actors  # the parent's context for world actors: choices, no faults
        self.actor: str | None = None
        # Draw keys carry the incarnation, namespaced per twin and for actors, so each replays alone.
        self.token = f"{incarnation}{f't{twin}' if twin else ''}{'a' if actors else ''}"
        self.quiet = quiet  # faults have stopped: the recovery incarnation draws nothing
        self.choices = choices
        self.lock = threading.Lock()
        self.counts: Counter[str] = Counter()
        self.seq = PARENT_SEQ + 1 if actors else 0
        self.world = JsonlWriter(workdir / WORLD)
        self.realized = JsonlWriter(workdir / REALIZED)
        self.weights = [NOMINAL_WEIGHT] + [1.0 if choices.enabled(f) else 0.0 for f in FAULTS]

    def append(self, writer: JsonlWriter, entry: dict) -> None:
        with self.lock:
            self.seq += 1
            writer.write({**entry, "inc": self.incarnation, "twin": self.twin, "seq": self.seq})

    def close(self) -> None:
        self.world.close()
        self.realized.close()

    def choose(self, label: str, options: Sequence[Any], weights: Sequence[float] | None,
               nominal: str | None = None) -> int:
        """Draw which of `options` the untrusted input picks at this point. 0 is nominal."""
        w = list(weights) if weights is not None else [NOMINAL_WEIGHT] + [1.0] * (len(options) - 1)
        if len(w) != len(options):
            raise ValueError(f"shape.choose({label!r}): {len(options)} options, {len(w)} weights")
        with self.lock:
            counter = f"choice:{label}"
            n = self.counts[counter]
            self.counts[counter] += 1
            draw_key = f"model:{label}#{self.token}.{n}"
            value = 0 if self.quiet else self.choices.draw(draw_key, w)
            if value:
                self.seq += 1
                self.realized.write({
                    "key": draw_key, "value": value, "choice": label,
                    "chosen": _short(options[value]), "nominal": nominal or _short(options[0]),
                    "actor": self.actor, "inc": self.incarnation, "twin": self.twin,
                    "seq": self.seq})
        return value

    def before(self, spec: EffectSpec, fn: Callable, sig: inspect.Signature,
               args: tuple, kwargs: dict) -> tuple | None:
        """Draw this call's fault and apply the ones that fire before the function runs. Returns
        what `after` needs, or None when the call is malformed (the function raises its own error)."""
        named = _named_args(sig, args, kwargs)
        if named is None:
            return None
        hidden = HIDDEN_ARGS | spec.hide
        shown = {k: v for k, v in named.items() if k not in hidden}
        label = fn.__qualname__.rsplit("<locals>.", 1)[-1]
        call = f"{label}({', '.join(f'{k}={_short(v)}' for k, v in shown.items())})"
        scope = spec.scope(**named) if spec.scope else ""
        key = spec.key(**named) if spec.key else None
        with self.lock:
            n = self.counts[spec.op]
            self.counts[spec.op] += 1
            draw_key = f"fault:{spec.op}#{self.token}.{n}"
            value = 0 if self.quiet or self.actors else self.choices.draw(draw_key, self.weights)
            fault = FAULTS[value - 1] if value else None
            error = make_error(fault, spec.errors) if fault and "crash" not in fault else None
            if error is not None:
                mark_injected(error)
            if fault:
                self.seq += 1
                self.realized.write({
                    "key": draw_key, "value": value, "fault": fault, "op": spec.op, "call": call,
                    "raised": type(error).__name__ if error else None,
                    "inc": self.incarnation, "twin": self.twin, "seq": self.seq})
        self.append(self.world, {"kind": "attempt", "op": spec.op, "scope": scope, "call": call,
                                 "fault": fault})
        if fault == "crash_before":
            os._exit(CRASH_EXIT)
        if error is not None and fault not in AFTER_COMMIT:
            raise error
        return spec, shown, call, scope, key, fault, error

    def after(self, pending: tuple, value: Any) -> None:
        """Record the effect that just landed, then apply the faults that fire after it."""
        spec, shown, call, scope, key, fault, error = pending
        self.append(self.world, {
            "kind": "effect", "op": spec.op, "scope": scope, "key": key, "call": call,
            "irreversible": spec.irreversible, "idempotent": spec.idempotent, "args": shown,
            "args_hash": args_hash(shown), "result": _result(spec, value),
            "fault": fault, "raised": type(error).__name__ if error else None})
        if fault == "crash_after":
            os._exit(CRASH_EXIT)
        if error is not None:
            raise error

    def call(self, spec: EffectSpec, fn: Callable, sig: inspect.Signature,
             args: tuple, kwargs: dict) -> Any:
        pending = self.before(spec, fn, sig, args, kwargs)
        if pending is None:
            return fn(*args, **kwargs)
        result = fn(*args, **kwargs)
        self.after(pending, result)
        return result

    async def acall(self, spec: EffectSpec, fn: Callable, sig: inspect.Signature,
                    args: tuple, kwargs: dict) -> Any:
        """`call` for `async def` functions: the effect is recorded when the awaited call returns,
        not when the coroutine is created."""
        pending = self.before(spec, fn, sig, args, kwargs)
        if pending is None:
            return await fn(*args, **kwargs)
        result = await fn(*args, **kwargs)
        self.after(pending, result)
        return result


_active: _Context | None = None


def activate(ctx: _Context | None) -> None:
    global _active
    _active = ctx


def effect(op: str, *, scope: Callable[..., str] | None = None,
           key: Callable[..., str | None] | None = None, irreversible: bool = False,
           errors: Mapping[str, Callable[[], Exception]] | None = None,
           idempotent: bool = False, result: Callable[[Any], Any] | None = None,
           hide: Sequence[str] = ()) -> Callable:
    """Mark a side-effecting function (sync or `async def`). Outside a shape run it is the plain
    function.

    `scope` names the business thing the effect applies to. `key` returns the idempotency key
    the call carries: effects with the same op, scope and key are one effect, because the
    provider dedupes them. `idempotent=True` makes repeating a call with the same scope and
    arguments one effect (a PUT-style write). `result` maps the return value to what the world
    log records as the effect's `result`, for invariants to read. `hide` names more arguments to
    leave out of the rendered call, on top of the ones frameworks inject. `errors` maps a fault
    name to a zero-argument exception factory.
    """
    spec = EffectSpec(op, scope, key, irreversible, dict(errors or {}), idempotent, result,
                      frozenset(hide))

    def wrap(fn: Callable) -> Callable:
        sig = inspect.signature(fn)

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                ctx = _active
                if ctx is None:
                    return await fn(*args, **kwargs)
                return await ctx.acall(spec, fn, sig, args, kwargs)
            return async_wrapper

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = _active
            if ctx is None:
                return fn(*args, **kwargs)
            return ctx.call(spec, fn, sig, args, kwargs)
        return wrapper
    return wrap


def choose(label: str, options: Sequence[Any], weights: Sequence[float] | None = None,
           nominal: str | None = None) -> Any:
    """Let shape pick what an untrusted input does here, usually your scripted model.

    `options[0]` is the nominal choice: what a well-behaved model would do. Inside a shape run,
    each seed may pick another option instead, and a failure shrinks back toward `options[0]`
    for every choice that was not needed to reproduce it. Outside a run, and on the last,
    fault-free incarnation, it returns `options[0]`. `weights` defaults to the nominal option
    being 10 times as likely as each alternative. `nominal` is how the trace names `options[0]`
    when its own text would not say what the model was supposed to do (the planned tool call,
    say); by default the option's repr is shown.

        action = shape.choose("after_tool_error", ["retry_once", "give_up", "retry_twice"])
    """
    if not options:
        raise ValueError(f"shape.choose({label!r}) needs at least one option")
    ctx = _active
    if ctx is None or len(options) == 1:
        return options[0]
    return options[ctx.choose(label, options, weights, nominal)]


def claim(scope: str, op: str) -> None:
    """Record that the agent told the user `op` happened for `scope` ("I've refunded order 17").

    Call it where the agent makes the statement. `shape.claims_true()` checks every claim has
    a matching effect."""
    ctx = _active
    if ctx is not None:
        ctx.append(ctx.world, {"kind": "claim", "scope": scope, "op": op})


def approval(scope: str, args: Mapping[str, Any] | None = None) -> None:
    """Record that a human (or policy) approved `scope` for exactly these effect arguments.

    Call it from the code that obtains the approval. `args` is the dict of named arguments of
    the effect being approved (without `self`)."""
    ctx = _active
    if ctx is not None:
        ctx.append(ctx.world, {"kind": "approval", "scope": scope,
                               "args_hash": args_hash(dict(args or {}))})
