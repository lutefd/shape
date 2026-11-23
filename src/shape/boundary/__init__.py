"""Boundary mode: test agent code you already have by faulting and killing its side effects."""

from .api import Run, Scenario, approval, effect, scenario
from .faults import RateLimited, ServerError
from .invariants import approved_before, at_most_once, exactly_once, scenario_terminates
from .runner import check

__all__ = [
    "RateLimited", "Run", "Scenario", "ServerError", "approval", "approved_before", "at_most_once",
    "check", "effect", "exactly_once", "scenario", "scenario_terminates",
]
