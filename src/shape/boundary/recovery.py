"""Deployment recovery policies over the boundary runner's existing restart loop."""

from __future__ import annotations

ACTIONS = {
    "redeliver": "redelivered the request",
    "langgraph-resume": "resumed the LangGraph thread from its checkpoint",
    "cron-retick": "cron re-ticked the job",
    "apscheduler-reload": "APScheduler reloaded and ran the job",
    "celery-acks-late": "Celery redelivered the unacknowledged task",
}
PRESETS = ("none", *ACTIONS)


def restarts_after(preset: str | None, outcome: str) -> bool:
    if preset == "none":
        return False
    if preset == "celery-acks-late":
        return outcome in ("crash", "timeout")
    return True
