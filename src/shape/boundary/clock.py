"""A virtual clock that code under test can opt into.

Inside a run the clock starts at `EPOCH` and only moves when something advances it: the runner
between incarnations (a recovery preset, `advance_on_restart=`), a world actor, or the scenario
itself (`run.advance(seconds)`). Outside a run it reads the real clock, so the same code works in
production. Real clocks are never replaced globally; `patched` swaps a module's own references.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import json
import os
import time as _time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

EPOCH = _dt.datetime(2025, 1, 1, tzinfo=_dt.UTC)
STATE = "clock.json"


def _workdir() -> Path | None:
    from . import api

    ctx = api._active
    return None if ctx is None else ctx.workdir


def offset(workdir: Path) -> float:
    """Virtual seconds since `EPOCH` in this run directory."""
    try:
        return float(json.loads((workdir / STATE).read_text())["offset"])
    except (OSError, ValueError, KeyError):
        return 0.0


def advance_dir(workdir: Path, seconds: float) -> float:
    """Move the run's clock forward and return the new offset. Safe across processes."""
    if seconds < 0:
        raise ValueError("a clock only moves forward; advance by a non-negative number of seconds")
    fd = os.open(workdir / (STATE + ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        total = offset(workdir) + float(seconds)
        tmp = workdir / (STATE + ".tmp")
        tmp.write_text(json.dumps({"offset": total}))
        os.replace(tmp, workdir / STATE)
        return total
    finally:
        os.close(fd)  # closing releases the lock


def stamp(seconds: float) -> str:
    return (EPOCH + _dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def now() -> _dt.datetime:
    """The current time, timezone-aware UTC: virtual inside a shape run, real outside one."""
    workdir = _workdir()
    if workdir is None:
        return _dt.datetime.now(_dt.UTC)
    return EPOCH + _dt.timedelta(seconds=offset(workdir))


def time() -> float:
    """`now()` as seconds since the Unix epoch."""
    return now().timestamp()


def monotonic() -> float:
    """A virtual monotonic reading: seconds since the run started (real `monotonic()` outside)."""
    workdir = _workdir()
    return _time.monotonic() if workdir is None else offset(workdir)


def sleep(seconds: float) -> None:
    """Advance the virtual clock instead of waiting (really sleeps outside a run)."""
    from . import api

    workdir = _workdir()
    if workdir is None:
        _time.sleep(seconds)
        return
    api._advance_clock(workdir, float(seconds), "sleep")


def advance(seconds: float) -> None:
    """Let `seconds` of virtual time pass. Outside a run this is an error: you cannot make
    real time pass faster."""
    from . import api

    workdir = _workdir()
    if workdir is None:
        raise RuntimeError("shape.clock.advance() only works inside a shape run")
    api._advance_clock(workdir, float(seconds), "advance")


# ------------------------------------------------------------------------------ patching a module


class _VirtualDatetime(_dt.datetime):
    """`datetime.datetime` whose "now" readings come from the virtual clock."""

    @classmethod
    def now(cls, tz: _dt.tzinfo | None = None) -> Any:
        value = now()
        return cls.fromtimestamp(value.timestamp(), tz) if tz else cls.fromtimestamp(
            value.timestamp(), _dt.UTC).replace(tzinfo=None)

    @classmethod
    def utcnow(cls) -> Any:
        return cls.now()

    @classmethod
    def today(cls) -> Any:
        return cls.now()


class _TimeModule(types.ModuleType):
    """Stands in for the `time` module: clock readings are virtual, the rest is the real module."""

    def __init__(self) -> None:
        super().__init__("time")

    def __getattr__(self, name: str) -> Any:
        return getattr(_time, name)

    time = staticmethod(time)
    monotonic = staticmethod(monotonic)
    perf_counter = staticmethod(monotonic)
    sleep = staticmethod(sleep)

    @staticmethod
    def time_ns() -> int:
        return int(time() * 1_000_000_000)

    @staticmethod
    def monotonic_ns() -> int:
        return int(monotonic() * 1_000_000_000)


class _DatetimeModule(types.ModuleType):
    """Stands in for the `datetime` module with a virtual `datetime.datetime`."""

    def __init__(self) -> None:
        super().__init__("datetime")

    def __getattr__(self, name: str) -> Any:
        return getattr(_dt, name)

    datetime = _VirtualDatetime


def _replacements() -> dict[int, Any]:
    time_module = _TimeModule()
    return {
        id(_time): time_module,
        id(_time.time): time,
        id(_time.monotonic): monotonic,
        id(_time.perf_counter): monotonic,
        id(_time.sleep): sleep,
        id(_time.time_ns): time_module.time_ns,
        id(_dt): _DatetimeModule(),
        id(_dt.datetime): _VirtualDatetime,
    }


@contextlib.contextmanager
def patched(*modules: types.ModuleType | str) -> Iterator[None]:
    """Point these modules' own `time` / `datetime` references at the virtual clock.

    Covers `import time`, `import datetime`, `from datetime import datetime` and
    `from time import time` (and `monotonic`, `sleep`, `perf_counter`) in the named modules'
    globals. Nothing else changes: other modules and the standard library keep the real clock.
    Entering patches outside a shape run still reads the real clock. Call it inside the scenario
    so every incarnation patches its own process:

        with shape.clock.patched(my_queue, my_worker):
            my_queue.run_once()
    """
    import importlib

    targets = [importlib.import_module(m) if isinstance(m, str) else m for m in modules]
    swaps = _replacements()
    undo: list[tuple[dict, str, Any]] = []
    try:
        for module in targets:
            namespace = vars(module)
            for name, value in list(namespace.items()):
                replacement = swaps.get(id(value))
                if replacement is not None:
                    undo.append((namespace, name, value))
                    namespace[name] = replacement
        yield
    finally:
        for namespace, name, value in reversed(undo):
            namespace[name] = value
