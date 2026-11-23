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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..choices import ChoiceSource
from . import clock as clock_module
from . import fixtures as fixture_module
from . import recovery as recovery_presets
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
    """What a scenario gets. `dir` survives crashes, so keep your own persistence in it.

    `fixtures` holds what the scenario's `@shape.fixture`s yielded (read it with `fixture(name)`).
    `scheduled` is the virtual time (seconds since `shape.clock.EPOCH`) of the tick this start was
    delivered for, when the recovery preset is a scheduler."""

    dir: Path
    restarts: int
    seed: int | None = None
    recovery: str | None = None
    fixtures: Mapping[str, Any] = field(default_factory=dict)
    scheduled: float | None = None

    def fixture(self, name: str) -> Any:
        """The value fixture `name` yielded for this seed (a DSN, a path)."""
        try:
            return self.fixtures[name]
        except KeyError:
            known = ", ".join(self.fixtures) or "none"
            raise KeyError(f"no fixture {name!r} in this run (fixtures: {known})") from None

    def unique(self, prefix: str = "shape") -> str:
        """A name no other seed or `--jobs` worker uses: for databases, queues, buckets."""
        return fixture_module.unique_name(self.dir, prefix)

    def advance(self, seconds: float) -> None:
        """Let `seconds` of virtual time pass (see `shape.clock`). A lease expires, a retry delay
        elapses, without a real wait."""
        _advance_clock(self.dir, seconds, "advance")

    def now(self) -> Any:
        """The virtual time in this run, as `shape.clock.now()` would read it."""
        import datetime

        return clock_module.EPOCH + datetime.timedelta(seconds=clock_module.offset(self.dir))

    @property
    def worker(self) -> str:
        """Which worker this start is: `worker-0` first, then `worker-1` after a takeover."""
        return f"worker-{self.restarts}"


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
    delivery: str | None = None


@dataclass(frozen=True)
class _PendingEffect:
    spec: EffectSpec
    shown: dict[str, Any]
    all_args: Mapping[str, Any]
    call: str
    scope: str
    key: str | None
    fault: str | None
    error: Exception | None
    attempt_key: str | None
    scope_source: str = "explicit"
    scope_rule: str | None = None


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
    net: Sequence[Any] | None = None
    strict_scopes: bool = False
    recovery: str | recovery_presets.Policy | None = None
    setup: Callable[[Run], None] | None = None
    fixtures: Sequence[fixture_module.Fixture] = ()
    delivery: Mapping[str, str] = field(default_factory=dict)
    advance_on_restart: float = 0.0

    def __call__(self, run: Run) -> None:
        self.fn(run)


DELIVERIES = ("at-least-once", "at-most-once")


def _check_delivery(op: str, delivery: str | None) -> None:
    if delivery is not None and delivery not in DELIVERIES:
        raise ValueError(f"delivery for {op!r} must be one of {', '.join(DELIVERIES)}; "
                         f"got {delivery!r}")


def scenario(fn: Callable[[Run], None] | None = None, *, invariants: Sequence[Any] = (),
             max_restarts: int = 3, actors: Sequence[Actor] = (),
             duplicate_trigger: bool = False, expect_effects: bool = True,
             concurrent: Sequence[Callable[[Run], None]] = (),
             net: Sequence[Any] | None = None, strict_scopes: bool = False,
             recovery: str | recovery_presets.Policy | None = None,
             setup: Callable[[Run], None] | None = None,
             fixtures: Sequence[fixture_module.Fixture] = (),
             delivery: Mapping[str, str] | None = None,
             advance_on_restart: float = 0.0) -> Any:
    """Declare a scenario.

    `setup(run)` runs once per seed before the first incarnation with no faults, crashes or model
    choices; its effects and network calls are logged as setup and not counted by invariants.
    `fixtures` are per-seed resources (`@shape.fixture`, `shape.fixtures.sqlite_copy`,
    `shape.fixtures.postgres_clone`). `delivery` maps an effect op to "at-most-once" for effects
    that are lossy by design (a lost one is reported separately, a duplicate still fails).
    `advance_on_restart` is virtual seconds that pass before every restart.
    """
    recovery_presets.validate(recovery)
    for op, mode in dict(delivery or {}).items():
        _check_delivery(op, mode)
    if advance_on_restart < 0:
        raise ValueError("advance_on_restart must be non-negative")
    declared = fixture_module.normalize(fixtures)

    def wrap(f: Callable[[Run], None]) -> Scenario:
        checks = tuple(invariants)
        fakes = None if net is None else tuple(net)
        if fakes is not None:
            from ..net import _providers

            _providers(fakes)
            checks += tuple(check for fake in fakes for check in fake.invariants())
        return Scenario(f, f.__name__, checks, max_restarts,
                        inspect.getsourcefile(f) or "", f.__module__, tuple(actors),
                        duplicate_trigger, expect_effects, tuple(concurrent), fakes,
                        strict_scopes, recovery, setup, declared, dict(delivery or {}),
                        float(advance_on_restart))
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


def _harness_fail_setup(ctx: _Context, fn: Callable, exc: Exception) -> None:
    msg = f"your setup raised {type(exc).__name__}: {exc}{_fn_location(fn, exc)}"
    ctx.append(ctx.world, {"kind": "harness_error", "op": "setup", "hook": "setup", "text": msg})


def declared_delivery(scen: Scenario, entries: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Each op's declared delivery guarantee: `@shape.effect(delivery=...)` as logged, then the
    scenario's own `delivery=` map (which wins)."""
    out = {e["op"]: e["delivery"] for e in entries if e["kind"] == "attempt" and e.get("delivery")}
    return {**out, **scen.delivery}


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
                 gate: Gate | None = None, actor_slot: int = 0, sync_logs: bool = True,
                 setup: bool = False) -> None:
        self.workdir = workdir
        self.setup = setup  # the fault-free phase before incarnation 0: entries are tagged setup
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
        self.async_points: dict[asyncio.Task, str] = {}
        self.seq = PARENT_SEQ + 1 if actors else 0
        self.phase = {"setup": True} if setup else {}
        self.world = JsonlWriter(workdir / WORLD, fsync=sync_logs)
        self.realized = JsonlWriter(workdir / REALIZED, fsync=sync_logs)
        self.weights = [NOMINAL_WEIGHT] + [1.0 if choices.enabled(f) else 0.0 for f in FAULTS]

    def append(self, writer: JsonlWriter, entry: dict) -> None:
        with self.lock:
            self._append(writer, entry)

    def _append(self, writer: JsonlWriter, entry: dict) -> None:
        self.seq += 1
        position = self.gate.position() if self.gate else {}
        writer.write({**entry, **position, **self.phase, "inc": self.incarnation,
                      "twin": self.twin, "seq": self.seq})

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
                    "chosen": self._task_label(index, ready[index]),
                    "nominal": self._task_label(0, ready[0])})
        return index

    def _task_label(self, index: int, handle: asyncio.Handle) -> str:
        task = getattr(handle._callback, "__self__", None)
        label = self.async_points.get(task) if isinstance(task, asyncio.Task) else None
        point = f" at {label!r}" if label else ""
        return f"callback {index} ({_callback_name(handle)}){point}"

    def before(self, spec: EffectSpec, fn: Callable, sig: inspect.Signature,
               args: tuple, kwargs: dict) -> _PendingEffect | None:
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
        if spec.scope:
            scope = _call_args_hook(self, "scope", spec.scope, spec.op, named, extra)
            scope_source, scope_rule = "explicit", None
        else:
            scope = ""
            scope_source, scope_rule = "effect_default", "empty"
        key = (
            _call_args_hook(self, "key", spec.key, spec.op, named, extra)
            if spec.key else None
        )
        return self.begin(spec, shown, all_args, call, scope, key,
                          scope_source=scope_source, scope_rule=scope_rule)

    def begin(self, spec: EffectSpec, shown: dict, all_args: Mapping[str, Any],
              call: str, scope: str, key: str | None, *,
              resolve_after: bool = False, scope_source: str = "explicit",
              scope_rule: str | None = None) -> _PendingEffect:
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
                    "raised": (getattr(error, "__shape_fault_label__", type(error).__name__)
                               if error is not None else None)})
        attempt = {
            "kind": "attempt", "op": spec.op, "scope": scope, "call": call, "fault": fault,
            "scope_source": scope_source, "scope_rule": scope_rule,
        }
        if spec.delivery:
            attempt["delivery"] = spec.delivery
        attempt_key = draw_key if resolve_after else None
        if attempt_key is not None:
            attempt["attempt_key"] = attempt_key
        self.append(self.world, attempt)
        if fault == "crash_before":
            os._exit(CRASH_EXIT)
        if error is not None and fault not in AFTER_COMMIT:
            raise error
        return _PendingEffect(spec, shown, all_args, call, scope, key, fault, error, attempt_key,
                              scope_source, scope_rule)

    def after(self, pending: _PendingEffect, value: Any) -> None:
        """Record the effect that just landed, then apply the faults that fire after it."""
        spec, scope, key = pending.spec, pending.scope, pending.key
        if spec.landed is not None:
            is_landed = bool(
                _call_result_hook(self, "landed", spec.landed, spec.op, value, pending.all_args)
            )
            if not is_landed:
                self.without_effect(pending, spec.op, scope,
                                    scope_source=pending.scope_source,
                                    scope_rule=pending.scope_rule)
                return
        if key is None and spec.key_from_result is not None:
            derived = _call_result_hook(
                self, "key_from_result", spec.key_from_result, spec.op, value, pending.all_args
            )
            if derived is not None:
                key = derived
        res = (
            _call_result_hook(self, "result", spec.result, spec.op, value, pending.all_args)
            if spec.result is not None else None
        )
        self.record_effect(pending, spec.op, scope, key, spec.irreversible, res,
                           scope_source=pending.scope_source, scope_rule=pending.scope_rule)

    def without_effect(self, pending: _PendingEffect, op: str, scope: str, *,
                       scope_source: str | None = None, scope_rule: str | None = None) -> None:
        entry = {
            "kind": "no_effect", "op": op, "scope": scope, "call": pending.call,
            "scope_source": pending.scope_source if scope_source is None else scope_source,
            "scope_rule": pending.scope_rule if scope_source is None else scope_rule,
        }
        if pending.attempt_key is not None:
            entry["attempt_key"] = pending.attempt_key
        self.append(self.world, entry)

    def record_effect(self, pending: _PendingEffect, op: str, scope: str, key: str | None,
                      irreversible: bool, result: Any, *,
                      scope_source: str | None = None, scope_rule: str | None = None) -> None:
        fault, error = pending.fault, pending.error
        entry = {
            "kind": "effect", "op": op, "scope": scope, "key": key, "call": pending.call,
            "irreversible": irreversible, "idempotent": pending.spec.idempotent,
            "args": pending.shown, "args_hash": args_hash(pending.shown), "result": result,
            "fault": fault, "raised": type(error).__name__ if error else None,
            "scope_source": pending.scope_source if scope_source is None else scope_source,
            "scope_rule": pending.scope_rule if scope_source is None else scope_rule,
        }
        if pending.attempt_key is not None:
            entry["attempt_key"] = pending.attempt_key
        self.append(self.world, entry)
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


async def _async_point(label: str) -> None:
    task = asyncio.current_task()
    ctx = _active
    if ctx is not None and task is not None:
        ctx.async_points[task] = label
        try:
            await asyncio.sleep(0)
        finally:
            ctx.async_points.pop(task, None)


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
           key_from_result: Callable[..., str | None] | None = None,
           delivery: str | None = None) -> Callable:
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
    name to a zero-argument exception factory. `delivery="at-most-once"` declares that losing
    this effect is by design (a fire-and-forget notification): `shape.exactly_once` then reports
    a lost one in its own "by design" section instead of as a defect, while a duplicate still
    fails.
    """
    _check_delivery(op, delivery)
    spec = EffectSpec(op, scope, key, irreversible, dict(errors or {}), idempotent, result,
                      frozenset(hide), landed, key_from_result, delivery)

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


def _note(text: str) -> None:
    """Record a harness-supplied action (such as on_restart='redeliver') so traces show it."""
    ctx = _active
    if ctx is not None:
        ctx.append(ctx.world, {"kind": "note", "text": text})


def approval(scope: str, args: Mapping[str, Any] | None = None) -> None:
    """Record that a human (or policy) approved `scope` for exactly these effect arguments.

    Call it from the code that obtains the approval. `args` is the dict of named arguments of
    the effect being approved (without `self`)."""
    ctx = _active
    if ctx is not None:
        ctx.append(ctx.world, {"kind": "approval", "scope": scope,
                               "args_hash": args_hash(dict(args or {}))})


def _advance_clock(workdir: Path, seconds: float, why: str) -> None:
    """Move the run's virtual clock and log it, so the trace shows where time passed."""
    total = clock_module.advance_dir(workdir, seconds)
    ctx = _active
    if ctx is not None and ctx.workdir == workdir and seconds:
        ctx.append(ctx.world, {"kind": "clock", "advance": seconds, "now": clock_module.stamp(total),
                               "why": why})
