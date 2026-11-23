"""Which exception to show when an incarnation dies: the one shape injected, or the innermost
one, not the framework wrapper (`ToolException`, `ExceptionGroup`, `RuntimeError("tool failed")`)
that the framework raised on top of it."""

from __future__ import annotations

import traceback

INJECTED = "__shape_injected__"


def mark_injected(exc: BaseException) -> BaseException:
    try:
        setattr(exc, INJECTED, True)
    except AttributeError:  # an exception type with __slots__ keeps its place in the chain
        pass
    return exc


def _next(exc: BaseException) -> BaseException | None:
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return exc.exceptions[0]
    if exc.__cause__ is not None:
        return exc.__cause__
    return None if exc.__suppress_context__ else exc.__context__


def root_cause(exc: BaseException) -> BaseException:
    """The first injected exception in the chain, else the innermost one."""
    seen: set[int] = set()
    current: BaseException | None = exc
    last = exc
    while current is not None and id(current) not in seen:
        if getattr(current, INJECTED, False):
            return current
        seen.add(id(current))
        last, current = current, _next(current)
    return last


def _line(exc: BaseException) -> str:
    if isinstance(exc, SyntaxError):  # its formatted form starts with the file and line
        return f"{type(exc).__name__}: {exc.msg}"
    text = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def describe(exc: BaseException) -> str:
    """`ReadTimeout: ...` for the root cause, plus what surfaced it when that is a wrapper."""
    root = root_cause(exc)
    if root is exc:
        return _line(exc)
    return f"{_line(root)} (surfaced as {_line(exc)})"
