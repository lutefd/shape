"""shape: deterministic simulation testing for agent systems."""

from .boundary import (
    Actor,
    ClientError,
    HarnessError,
    RateLimited,
    Run,
    ServerError,
    World,
    actor,
    approval,
    approved_before,
    at_most_once,
    check,
    choose,
    claim,
    claims_true,
    effect,
    exactly_once,
    invariant,
    max_calls,
    point,
    replay,
    requires,
    run_async,
    scenario,
    scenario_terminates,
    sum_le,
    within_scopes,
)

__all__ = [
    "Actor",
    "ClientError",
    "HarnessError",
    "RateLimited",
    "Run",
    "ServerError",
    "World",
    "actor",
    "approval",
    "approved_before",
    "at_most_once",
    "check",
    "choose",
    "claim",
    "claims_true",
    "effect",
    "exactly_once",
    "invariant",
    "max_calls",
    "net",
    "point",
    "replay",
    "requires",
    "run_async",
    "scenario",
    "scenario_terminates",
    "sum_le",
    "within_scopes",
]


def __getattr__(name: str):
    if name in ("langgraph", "agents", "fakes", "net"):
        import importlib
        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module 'shape' has no attribute {name!r}")
