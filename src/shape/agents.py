"""OpenAI Agents SDK adapter (lazy re-export of `shape.adapters.agents`)."""

from .adapters.agents import (
    Call,
    Effect,
    ScriptedModel,
    drive,
    effects,
)

__all__ = ["Call", "Effect", "ScriptedModel", "drive", "effects"]
