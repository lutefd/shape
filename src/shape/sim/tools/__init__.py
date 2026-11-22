"""Verified-fake candidates for the services the toy runtime talks to.

A fake models only what the invariants depend on: resource state machines, idempotency
behavior, error classes, and visibility. Each states the real API it mirrors. None has been
checked against the real sandbox yet (the contract suite is planned after milestone 1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolSpec:
    """What an agent may call. `schema` maps argument name to "int" or "str"."""

    tool: str
    op: str
    schema: dict[str, str]
    irreversible: bool = False


class Tool(Protocol):
    name: str
    api: str  # the real API this fake mirrors
    specs: tuple[ToolSpec, ...]

    def execute(self, op: str, args: dict[str, Any], key: str | None) -> dict[str, Any]:
        """Apply the call to the world. Raises ApiError for a definite rejection."""
        ...


from .approvals import SimApprovals
from .db import SimDB
from .email import SimEmail
from .payments import SimPayments

__all__ = ["SimApprovals", "SimDB", "SimEmail", "SimPayments", "Tool", "ToolSpec"]
