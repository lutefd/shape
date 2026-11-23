"""The user-facing API: `effect`, `approval`, `scenario`, `Run`, and the per-incarnation context
that every wrapped call goes through."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
import os
import threading
import traceback
from collections import Counter, deque
from collections.abc import Callable, Coroutine, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..choices import ChoiceSource
from .asyncio import BoundaryLoop
from .faults import (
    AFTER_COMMIT,
    CRASH_EXIT,
    FAULTS,
    NOMINAL_WEIGHT,
    make_error,
)
from .log import PARENT_SEQ, REALIZED, WORLD, JsonlWriter, args_hash
from .rootcause import mark_injected
from .schedule import Gate

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


class HarnessError(BaseException):
    """A user-supplied shape hook (`scope`, `key`, `result`, `landed`, `key_from_result`) raised.

    Subclasses `BaseException` rather than `Exception` so agent frameworks (Agents SDK error
    handlers, LangGraph `ToolNode(handle_tool_errors=True)`) cannot swallow it as a tool failure.
    """

    def __init__(self, hook: str, op: str, message: str) -> None:
        self.hook = hook
        self.op = op
        super().__init__(message)


_extra_context: contextvars.ContextVar[Mapping[str, Any] | None] = contextvars.ContextVar(
    "shape_extra_context", default=None
)


@contextlib.contextmanager
def with_context(extra: Mapping[str, Any] | None) -> Iterator[None]:
    """Attach adapter-level framework context (e.g. Agents SDK `ctx`, LangGraph `config`) to the
    current call so `scope`, `key`, `result`, `landed`, and `key_from_result` receive it without
    changing the tool's signature or rendered call."""
    if not extra:
        yield
        return
    prev = _extra_context.get()
    merged = {**(prev or {}), **extra}
    token = _extra_context.set(merged)
    try:
        yield
    finally:
        _extra_context.reset(token)


@dataclass(frozen=True)
class EffectSpec:
    op: str
    scope: Callable[..., str] | None
    key: Callable[..., str | None] | None
    irreversible: bool
    errors: Mapping[str, Callable[[], Exception]]
    idempotent: bool = False
    result: Callable[..., Any] | None = None
    hide: frozenset[str] = frozenset()
    landed: Callable[..., Any] | None = None
    key_from_result: Callable[..., str | None] | None = None


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
    concurrent: Sequence[Callable[[Run], None]] = ()

    def __call__(self, run: Run) -> None:
        self.fn(run)


def scenario(fn: Callable[[Run], None] | None = None, *, invariants: Sequence[Any] = (),
             max_restarts: int = 3, actors: Sequence[Actor] = (),
             duplicate_trigger: bool = False, expect_effects: bool = True,
             concurrent: Sequence[Callable[[Run], None]] = ()) -> Any:
    def wrap(f: Callable[[Run], None]) -> Scenario:
        return Scenario(f, f.__name__, tuple(invariants), max_restarts,
                        inspect.getsourcefile(f) or "", f.__module__, tuple(actors),
                        duplicate_trigger, expect_effects, tuple(concurrent))
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


def _callback_name(handle: asyncio.Handle) -> str:
    callback = handle._callback
    task = getattr(callback, "__self__", None)
    if isinstance(task, asyncio.Task):
        name = task.get_name()
        if not (name.startswith("Task-") and name[5:].isdigit()):
            return name
        coro = task.get_coro()
        return getattr(coro, "__qualname__", type(coro).__name__).rsplit("<locals>.", 1)[-1]
    return getattr(callback, "__qualname__", type(callback).__name__)


def _fn_location(fn: Callable, exc: BaseException) -> str:
    """Point at the user's hook (`file.py:42`): traceback frame inside the hook if it ran, else
    the hook's definition line (for signature TypeErrors before the body enters)."""
    target = getattr(fn, "__shape_user_fn__", fn)
    tb = traceback.extract_tb(exc.__traceback__)
    for frame in reversed(tb):
        if frame.filename != __file__ and not frame.filename.endswith(
            ("adapters/_plan.py", "adapters/agents.py", "adapters/langgraph.py")
        ):
            return f" ({Path(frame.filename).name}:{frame.lineno})"
    try:
        src = inspect.getsourcefile(target)
        _, lineno = inspect.getsourcelines(target)
        if src:
            return f" ({Path(src).name}:{lineno})"
    except (OSError, TypeError):
        pass
    return ""


def _invoke_args_callable(fn: Callable, named: Mapping[str, Any], extra: Mapping[str, Any]) -> Any:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(**named)
    params = sig.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(**{**extra, **named})
    call_kw = dict(named)
    for k, v in extra.items():
        if k in params and k not in call_kw:
            call_kw[k] = v
    return fn(**call_kw)


def _invoke_result_callable(fn: Callable, value: Any, all_args: Mapping[str, Any]) -> Any:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(value)
    params = list(sig.parameters.values())
    has_varkw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params)
    first_name = (
        params[0].name
        if params and params[0].kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        else None
    )
    if has_varkw:
        kw = {k: v for k, v in all_args.items() if k != first_name}
        return fn(value, **kw)
    start = 1 if params and params[0].kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.VAR_POSITIONAL,
    ) else 0
    extra_names = [
        p.name
        for p in params[start:]
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ]
    if extra_names:
        kw = {k: all_args[k] for k in extra_names if k in all_args}
        return fn(value, **kw)
    return fn(value)


def _harness_fail(ctx: _Context, hook: str, fn: Callable, op: str, exc: Exception) -> None:
    msg = f"your {hook} for {op} raised {type(exc).__name__}: {exc}{_fn_location(fn, exc)}"
    ctx.append(ctx.world, {"kind": "harness_error", "op": op, "hook": hook, "text": msg})
    raise HarnessError(hook, op, msg) from exc


def _call_args_hook(ctx: _Context, hook: str, fn: Callable, op: str,
                    named: Mapping[str, Any], extra: Mapping[str, Any]) -> Any:
    try:
        return _invoke_args_callable(fn, named, extra)
    except Exception as exc:  # noqa: BLE001 - user hook error must fail loudly, never silently
        _harness_fail(ctx, hook, fn, op, exc)


def _call_result_hook(ctx: _Context, hook: str, fn: Callable, op: str,
                      value: Any, all_args: Mapping[str, Any]) -> Any:
    try:
        return _invoke_result_callable(fn, value, all_args)
    except Exception as exc:  # noqa: BLE001 - user hook error must fail loudly, never silently
        _harness_fail(ctx, hook, fn, op, exc)


class _Context:
    """One incarnation's view of the run. Counters, draws and log appends share one lock, so a
    framework that runs sync tools in a thread pool still gets stable per-op keys."""

    def __init__(self, workdir: Path, incarnation: int, choices: ChoiceSource,
                 quiet: bool = False, twin: int = 0, actors: bool = False, *,
                 gate: Gate | None = None, actor_slot: int = 0) -> None:
        self.incarnation = incarnation
        self.twin = twin  # 1 for the duplicate delivery of incarnation 0
        self.actors = actors  # the parent's context for world actors: choices, no faults
        self.actor: str | None = None
        # Draw keys carry the incarnation, namespaced per twin and for actors, so each replays alone.
        self.token = f"{incarnation}{f't{twin}' if twin else ''}{'a' if actors else ''}"
        if actor_slot:
            self.token += f"p{actor_slot}"
        self.gate = gate
        self.watchdog: threading.Timer | None = None
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
            self._append(writer, entry)

    def _append(self, writer: JsonlWriter, entry: dict) -> None:
        self.seq += 1
        position = self.gate.position() if self.gate else {}
        writer.write({**entry, **position, "inc": self.incarnation, "twin": self.twin,
                      "seq": self.seq})

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
                self._append(self.realized, {
                    "key": draw_key, "value": value, "choice": label,
                    "chosen": _short(options[value]), "nominal": nominal or _short(options[0]),
                    "actor": self.actor})
        return value

    def pick_task(self, ready: deque[asyncio.Handle]) -> int:
        if self.quiet:
            return 0
        n = len(ready)
        with self.lock:
            counter = self.counts["sched:tasks"]
            self.counts["sched:tasks"] += 1
            key = f"sched:tasks#{self.token}.{counter}"
            weight = 1.0 if self.choices.enabled("sched") else 0.0
            index = self.choices.draw(key, [3.0] + [weight / (n - 1)] * (n - 1))
            if index:
                self._append(self.realized, {
                    "key": key, "value": index, "schedule": "tasks",
                    "chosen": f"callback {index} ({_callback_name(ready[index])})",
                    "nominal": f"callback 0 ({_callback_name(ready[0])})"})
        return index

    def before(self, spec: EffectSpec, fn: Callable, sig: inspect.Signature,
               args: tuple, kwargs: dict) -> tuple | None:
        """Draw this call's fault and apply the ones that fire before the function runs. Returns
        what `after` needs, or None when the call is malformed (the function raises its own error)."""
        named = _named_args(sig, args, kwargs)
        if named is None:
            if self.gate:
                self.gate.point(fn.__qualname__)
            return None  # let the real function raise its own error
        extra = _extra_context.get() or {}
        all_args = {**extra, **named}
        hidden = HIDDEN_ARGS | spec.hide
        shown = {k: v for k, v in named.items() if k not in hidden}
        label = fn.__qualname__.rsplit("<locals>.", 1)[-1]
        call = f"{label}({', '.join(f'{k}={_short(v)}' for k, v in shown.items())})"
        if self.gate:
            self.gate.point(call)
        scope = (
            _call_args_hook(self, "scope", spec.scope, spec.op, named, extra)
            if spec.scope else ""
        )
        key = (
            _call_args_hook(self, "key", spec.key, spec.op, named, extra)
            if spec.key else None
        )
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
                self._append(self.realized, {
                    "key": draw_key, "value": value, "fault": fault, "op": spec.op, "call": call,
                    "raised": type(error).__name__ if error else None})
        self.append(self.world, {"kind": "attempt", "op": spec.op, "scope": scope, "call": call,
                                 "fault": fault})
        if fault == "crash_before":
            os._exit(CRASH_EXIT)
        if error is not None and fault not in AFTER_COMMIT:
            raise error
        return spec, shown, all_args, call, scope, key, fault, error

    def after(self, pending: tuple, value: Any) -> None:
        """Record the effect that just landed, then apply the faults that fire after it."""
        spec, shown, all_args, call, scope, key, fault, error = pending
        if spec.landed is not None:
            is_landed = bool(
                _call_result_hook(self, "landed", spec.landed, spec.op, value, all_args)
            )
            if not is_landed:
                self.append(self.world, {
                    "kind": "no_effect", "op": spec.op, "scope": scope, "call": call,
                })
                return
        if key is None and spec.key_from_result is not None:
            derived = _call_result_hook(
                self, "key_from_result", spec.key_from_result, spec.op, value, all_args
            )
            if derived is not None:
                key = derived
        res = (
            _call_result_hook(self, "result", spec.result, spec.op, value, all_args)
            if spec.result is not None else None
        )
        self.append(self.world, {
            "kind": "effect", "op": spec.op, "scope": scope, "key": key, "call": call,
            "irreversible": spec.irreversible, "idempotent": spec.idempotent, "args": shown,
            "args_hash": args_hash(shown), "result": res,
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


def point(label: str) -> None:
    """Yield the process turn before a shared-state operation."""
    ctx = _active
    if ctx is not None and ctx.gate is not None:
        ctx.gate.point(label, show=True)


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run an asyncio coroutine with seeded scheduling inside a shape boundary run."""
    ctx = _active
    if ctx is None:
        return asyncio.run(coro)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("shape.run_async() cannot be called from a running event loop")
    with asyncio.Runner(loop_factory=lambda: BoundaryLoop(ctx)) as runner:
        return runner.run(coro)


def effect(op: str, *, scope: Callable[..., str] | None = None,
           key: Callable[..., str | None] | None = None, irreversible: bool = False,
           errors: Mapping[str, Callable[[], Exception]] | None = None,
           idempotent: bool = False, result: Callable[..., Any] | None = None,
           hide: Sequence[str] = (), landed: Callable[..., Any] | None = None,
           key_from_result: Callable[..., str | None] | None = None) -> Callable:
    """Mark a side-effecting function (sync or `async def`). Outside a shape run it is the plain
    function.

    `scope` names the business thing the effect applies to. `key` returns the idempotency key
    the call carries: effects with the same op, scope and key are one effect, because the
    provider dedupes them. `key_from_result` derives a dedupe key from the return value (for tools
    that dedupe via state and return the same resource id on a repeat call). `landed` inspects the
    return value and returns False when the call returned without side effect (e.g. a refusal
    string or error dict). `idempotent=True` makes repeating a call with the same scope and
    arguments one effect (a PUT-style write). `result` maps the return value to what the world
    log records as the effect's `result`, for invariants to read. `hide` names more arguments to
    leave out of the rendered call, on top of the ones frameworks inject. `errors` maps a fault
    name to a zero-argument exception factory.
    """
    spec = EffectSpec(op, scope, key, irreversible, dict(errors or {}), idempotent, result,
                      frozenset(hide), landed, key_from_result)

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


def claim(scope: str, op: str, *, ok: bool = True) -> None:
    """Record what the agent told the user about `op` for `scope`.

    `ok=True` (default): "I've refunded order 17" — `shape.claims_true()` checks an effect landed.
    `ok=False`: "I couldn't refund order 17" — `shape.claims_true()` checks no effect landed, and
    `shape.exactly_once(op, failure_claim_ok=True)` accepts a lost effect when the agent explicitly
    reported failure for that scope.
    """
    ctx = _active
    if ctx is not None:
        ctx.append(ctx.world, {"kind": "claim", "scope": scope, "op": op, "ok": bool(ok)})


def approval(scope: str, args: Mapping[str, Any] | None = None) -> None:
    """Record that a human (or policy) approved `scope` for exactly these effect arguments.

    Call it from the code that obtains the approval. `args` is the dict of named arguments of
    the effect being approved (without `self`)."""
    ctx = _active
    if ctx is not None:
        ctx.append(ctx.world, {"kind": "approval", "scope": scope,
                               "args_hash": args_hash(dict(args or {}))})
