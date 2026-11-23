"""The fault table. Each fault kind carries its provenance, so a finding can say whether its
minimized trace needs only documented behavior or leans on a theoretical fault.

Provenance:
* documented    the provider's own docs describe it
* observed      seen in practice, not guaranteed by docs
* theoretical   possible by the model of the system, no report of it
"""

from __future__ import annotations

from dataclasses import dataclass


class Crash(BaseException):
    """The agent process dies. A BaseException so `except Exception` cannot swallow it."""


class ToolError(Exception):
    """Base class for what a tool call can raise."""


class ToolTimeout(ToolError):
    """No response. The effect may or may not have happened."""


class RateLimited(ToolError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class ServerError(ToolError):
    """Transient 5xx. The request did not take effect."""


class ApiError(ToolError):
    """A definite answer: the request was rejected."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Fault:
    name: str
    provenance: str
    weight: float  # relative to the nominal outcome's weight of 1.0
    ops: str  # "*" for any call, "compensation" for compensating calls only, "actor" for outsiders
    note: str


FAULTS: tuple[Fault, ...] = (
    Fault("latency", "documented", 0.05, "*", "slow response; no semantic change"),
    Fault("timeout", "documented", 0.03, "*", "client gives up before the request commits"),
    Fault("timeout_after_commit", "documented", 0.03, "*",
          "request commits, the response is lost (Stripe idempotency docs: 'connection errors')"),
    Fault("duplicate_delivery", "theoretical", 0.02, "*", "an intermediary delivers a request twice"),
    Fault("rate_limit", "documented", 0.03, "*", "429 with retry-after; no effect"),
    Fault("transient_5xx", "documented", 0.03, "*", "500/503 before commit; no effect"),
    Fault("compensation_failure", "documented", 0.15, "compensation",
          "a compensating call is rejected (e.g. refund processing_error)"),
    Fault("rate_limit_burst", "theoretical", 0.08, "*",
          "429s arrive in runs: a shared quota stays exhausted past the advertised retry-after, "
          "so the next calls to the same op are limited too; no effect"),
    Fault("outage_burst", "theoretical", 0.08, "*",
          "5xx arrive in runs: a dependency outage outlasts a few retries; no effect"),
)
CALL_FAULTS = tuple(f.name for f in FAULTS)
PROVENANCE = {f.name: f.provenance for f in FAULTS} | {
    "external_modification": "documented",
    "crash": "documented",
}
# Correlated faults: a burst fault hits the call that drew it and then the next calls to the same
# op, with the base fault's behavior. `burst:<tool>.<op>#<n>` draws the length; 0 is the shortest.
BURSTS = {"rate_limit_burst": "rate_limit", "outage_burst": "transient_5xx"}
BURST_LENGTHS = (2, 3, 4, 6, 8)
CRASH_WEIGHT = 0.01  # per durable-write crash point, each of the two crash positions
# A tool call is in flight for real time (latency, then the response) while a durable write is
# instantaneous, so a process dies inside a call more often than inside a write.
CALL_CRASH_WEIGHT = 0.05
ACTOR_WEIGHT = 0.04  # per actor tick
MAX_CRASHES = 3


def call_weights(enabled, compensating: bool) -> list[float]:
    """Weights over [nominal, *FAULTS] for one call. Disabled or inapplicable kinds get 0."""
    out = [1.0]
    for f in FAULTS:
        applies = f.ops == "*" or (f.ops == "compensation" and compensating)
        out.append(f.weight if applies and enabled(f.name) else 0.0)
    return out
