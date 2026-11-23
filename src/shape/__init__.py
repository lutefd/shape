"""shape: deterministic simulation testing for agent systems."""

from .boundary import (
    RateLimited,
    Run,
    ServerError,
    approval,
    approved_before,
    at_most_once,
    check,
    choose,
    effect,
    exactly_once,
    scenario,
    scenario_terminates,
)

__all__ = [
    "RateLimited", "Run", "ServerError", "approval", "approved_before", "at_most_once", "check",
    "choose", "effect", "exactly_once", "scenario", "scenario_terminates",
]


def __getattr__(name: str):
    if name in ("langgraph", "agents"):
        import importlib
        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module 'shape' has no attribute {name!r}")
