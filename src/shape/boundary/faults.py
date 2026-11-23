"""The boundary fault table and the exceptions a fault raises.

The exception a fault raises is what the user's stack would really see, so each
`@shape.effect` may supply its own factories. Retry behavior in the code under test depends
on the exception type.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

# Index into [nominal, *FAULTS] is the value drawn at `fault:<op>#<incarnation>.<n>`.
FAULTS = (
    "timeout",
    "timeout_after_commit",
    "connection_reset_after_commit",
    "rate_limit",
    "server_error",
    "crash_before",
    "crash_after",
    "client_error",  # last: older replay files index into this table
)
NOMINAL_WEIGHT = 10.0
CRASHES = frozenset({"crash_before", "crash_after"})
AFTER_COMMIT = frozenset({"timeout_after_commit", "connection_reset_after_commit", "crash_after"})
CRASH_EXIT = 137

# What each fault means in a trace, in plain words.
FAULT_TEXT = {
    "timeout": "timeout before the effect: the caller saw {exc}",
    "timeout_after_commit": "timeout after commit: the caller saw {exc}",
    "connection_reset_after_commit": "connection reset after commit: the caller saw {exc}",
    "rate_limit": "rate limited before the effect: the caller saw {exc}",
    "server_error": "server error before the effect: the caller saw {exc}",
    "client_error": "client error (4xx) before the effect: the caller saw {exc}",
    "crash_before": "process killed before the call ran",
    "crash_after": "process killed after the effect landed, before the caller saw it",
    "duplicate_trigger": "delivered twice: two copies ran at the same time on the same run dir",
}

# A scenario-level fault, drawn once per seed by the runner instead of once per call. It is not in
# FAULTS because it has no per-call value; its key is `world:duplicate_trigger`.
DUPLICATE_TRIGGER = "duplicate_trigger"
DUPLICATE_KEY = f"world:{DUPLICATE_TRIGGER}"


class RateLimited(Exception):
    """Default exception for the `rate_limit` fault (an HTTP 429)."""


class ServerError(Exception):
    """Default exception for the `server_error` fault (an HTTP 5xx)."""


class ClientError(Exception):
    """Default exception for the `client_error` fault (an HTTP 4xx other than 429)."""


def _default(fault: str) -> Exception:
    message = f"shape: injected {fault}"
    if fault in ("timeout", "timeout_after_commit"):
        return TimeoutError(message)
    if fault == "connection_reset_after_commit":
        return ConnectionResetError(message)
    if fault == "rate_limit":
        return RateLimited(message)
    if fault == "client_error":
        return ClientError(message)
    return ServerError(message)


def make_error(fault: str, errors: Mapping[str, Callable[[], Exception]]) -> Exception:
    """Build the exception for `fault`. A `timeout` factory also serves `timeout_after_commit`."""
    factory = errors.get(fault)
    if factory is None and fault == "timeout_after_commit":
        factory = errors.get("timeout")
    return factory() if factory else _default(fault)
