"""Deployment recovery policies over the boundary runner's restart loop.

A policy answers one question when an incarnation dies: does the deployment bring the work back,
after how much time, and with which scheduled runs? Each preset that names a real system enforces
that system's rule instead of only rerunning the scenario:

- `apscheduler-reload`: APScheduler 3.x moves a job's `next_run_time` forward when it submits the
  job, so the tick that crashed is not rerun. The next due tick runs, subject to
  `misfire_grace_time` and `coalesce`. A one-shot (`date`) job is removed at submit and is lost.
- `celery-acks-late`: a task whose pool child dies is redelivered only with
  `task_reject_on_worker_lost=True`; a task that raised is acknowledged and not redelivered.
- `lease-takeover`: another worker reclaims the job only after the dead worker's lease expired.

Times are virtual seconds since `shape.clock.EPOCH`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

CRASHED = ("crash", "timeout")


@dataclass(frozen=True)
class Died:
    """An incarnation that just ended without finishing."""

    inc: int  # its index, 0 for the first start
    outcome: str  # "crash", "timeout" or "error" (it raised)
    started: float  # virtual time it started
    at: float  # virtual time it died
    scheduled: float  # the tick it was started for


@dataclass(frozen=True)
class Resume:
    """What the deployment does next: wait `advance` seconds, then start the scenario once for
    each time in `fires` (the ticks the scenario is delivered, in order)."""

    advance: float = 0.0
    fires: tuple[float, ...] = ()
    note: str = ""


class Policy:
    """Base class. `name` appears in traces, `action` is the line the trace shows on a restart."""

    name: str | None = None
    action: str | None = None
    verified: str = "unverified"

    def resume(self, died: Died) -> Resume | None:
        """None means nothing brings the work back: the run ends there."""
        raise NotImplementedError


@dataclass(frozen=True)
class Default(Policy):
    """No `recovery=`: the runner restarts after every death, as it always has."""

    def resume(self, died: Died) -> Resume | None:
        return Resume()


@dataclass(frozen=True)
class NoRecovery(Policy):
    name = "none"
    action = None
    verified = "n/a"

    def resume(self, died: Died) -> Resume | None:
        return None


@dataclass(frozen=True)
class Redeliver(Policy):
    """An at-least-once queue hands the same job to a new worker after any failed attempt."""

    name = "redeliver"
    action = "redelivered the request"
    verified = "documented (RabbitMQ requeueing)"
    delay: float = 0.0

    def resume(self, died: Died) -> Resume | None:
        return Resume(advance=self.delay)


@dataclass(frozen=True)
class LangGraphResume(Policy):
    name = "langgraph-resume"
    action = "resumed the LangGraph thread from its checkpoint"
    verified = "driver tests (langgraph checkpointer)"
    delay: float = 0.0

    def resume(self, died: Died) -> Resume | None:
        return Resume(advance=self.delay)


@dataclass(frozen=True)
class CronRetick(Policy):
    """The next scheduled tick starts a fresh invocation. `every` is the period in seconds."""

    name = "cron-retick"
    action = "cron re-ticked the job"
    verified = "documented (Kubernetes CronJob)"
    every: float = 0.0

    def resume(self, died: Died) -> Resume | None:
        return Resume(advance=self.every)


@dataclass(frozen=True)
class APSchedulerReload(Policy):
    """APScheduler 3.x with a persistent job store, after the process running a job died.

    `interval` is the period of a recurring job (interval or fixed-period cron trigger) in
    seconds; None is a one-shot `date` job. `downtime` is how long the process stays down.
    `misfire_grace_time` (None means no limit) and `coalesce` are the job's settings, with
    APScheduler's defaults (1 second, True).

    The scheduler advances `next_run_time` as it submits a job, so the tick that crashed is
    never rerun. On restart, every tick that came due while the process was down is delivered
    (only the latest when `coalesce`), minus those later than `misfire_grace_time`. If none
    remains, the job waits for the next tick.
    """

    name = "apscheduler-reload"
    action = "APScheduler reloaded the job store; the crashed tick was not rerun"
    verified = "verified against APScheduler 3.11 (tests/boundary/test_preset_apscheduler.py)"
    interval: float | None = None
    downtime: float = 0.0
    misfire_grace_time: float | None = 1.0
    coalesce: bool = True

    def resume(self, died: Died) -> Resume | None:
        if self.interval is None:
            return None  # a date job is removed from the store when it is submitted
        step = float(self.interval)
        if step <= 0:
            raise ValueError("APSchedulerReload.interval must be positive")
        # A process that raised is still up and waiting for the next tick; a dead one is down.
        restart = died.at + (max(0.0, self.downtime) if died.outcome in CRASHED else 0.0)
        last = died.scheduled
        owed = math.floor((restart - last) / step + 1e-9)  # ticks that came due: last + k*step
        due = [last + k * step for k in range(max(1, owed - 999), owed + 1)]  # newest 1000
        if self.coalesce:
            due = due[-1:]
        delivered = [t for t in due
                     if self.misfire_grace_time is None or restart - t <= self.misfire_grace_time]
        if delivered:
            return Resume(advance=restart - died.at, fires=tuple(delivered))
        following = (due[-1] if due else last) + step
        return Resume(advance=following - died.at, fires=(following,))


@dataclass(frozen=True)
class CeleryAcksLate(Policy):
    """Celery with `acks_late=True`, after the process running a task died.

    `lost="child"` (the default) is a pool child that died while its parent worker lives (an
    OOM kill, a `SIGKILL`, `os._exit`): Celery acknowledges the task and the work is lost unless
    `reject_on_worker_lost` (`task_reject_on_worker_lost=True`) requeues it. `lost="worker"` is
    the whole worker (a killed container): the unacknowledged message returns to the queue, at
    once on RabbitMQ and after `visibility_timeout` on Redis. A task that raised is acknowledged
    (`task_acks_on_failure_or_timeout`) and never redelivered.
    """

    name = "celery-acks-late"
    action = "Celery redelivered the unacknowledged task"
    verified = "verified against Celery 5.7 (tests/boundary/test_preset_celery.py)"
    lost: str = "child"
    reject_on_worker_lost: bool = False
    broker: str = "rabbitmq"
    visibility_timeout: float = 3600.0

    def __post_init__(self) -> None:
        if self.lost not in ("child", "worker"):
            raise ValueError("CeleryAcksLate.lost must be 'child' or 'worker'")
        if self.broker not in ("rabbitmq", "redis"):
            raise ValueError("CeleryAcksLate.broker must be 'rabbitmq' or 'redis'")

    def resume(self, died: Died) -> Resume | None:
        if died.outcome not in CRASHED:
            return None  # the task raised: acknowledged, not redelivered
        if self.lost == "child":
            if died.outcome == "timeout" or not self.reject_on_worker_lost:
                return None  # acknowledged on worker loss / time limit: the task is gone
            return Resume()
        return Resume(advance=self.visibility_timeout if self.broker == "redis" else 0.0)


@dataclass(frozen=True)
class LeaseTakeover(Policy):
    """A job queue where a worker holds a lease on the job it runs.

    A dead worker cannot release or renew its lease, so another worker reclaims the job only
    once `lease_seconds` have passed, and a reaper that polls every `reclaim_interval` seconds
    (default 1) notices. Until then the job stays leased and nothing runs. A job that raised is the live worker's to fail
    or retry, not a takeover.
    """

    name = "lease-takeover"
    action = "another worker reclaimed the job after its lease expired"
    verified = "unverified: models a common pattern, no library to check it against"
    lease_seconds: float = 30.0
    reclaim_interval: float = 1.0

    def resume(self, died: Died) -> Resume | None:
        if died.outcome not in CRASHED:
            return None
        return Resume(advance=self.lease_seconds + max(0.0, self.reclaim_interval))


PRESET_POLICIES: dict[str, Policy] = {
    "none": NoRecovery(),
    "redeliver": Redeliver(),
    "langgraph-resume": LangGraphResume(),
    "cron-retick": CronRetick(),
    "apscheduler-reload": APSchedulerReload(),
    "celery-acks-late": CeleryAcksLate(),
    "lease-takeover": LeaseTakeover(),
}
ACTIONS = {name: p.action for name, p in PRESET_POLICIES.items() if p.action}
PRESETS = tuple(PRESET_POLICIES)
_DEFAULT = Default()


def resolve(recovery: str | Policy | None) -> Policy:
    """The policy for a scenario's `recovery=`: a preset name, a policy object or None."""
    if recovery is None:
        return _DEFAULT
    if isinstance(recovery, Policy):
        return recovery
    return PRESET_POLICIES[recovery]


def name_of(recovery: str | Policy | None) -> str | None:
    return recovery.name if isinstance(recovery, Policy) else recovery


def validate(recovery: str | Policy | None) -> None:
    if recovery is None or isinstance(recovery, Policy):
        return
    if recovery not in PRESET_POLICIES:
        choices = ", ".join(PRESETS)
        raise ValueError(f"unknown recovery preset {recovery!r}; choose from {choices}")


def restarts_after(preset: str | Policy | None, outcome: str) -> bool:
    """Whether a death with this outcome is followed by a restart under `preset`."""
    return resolve(preset).resume(Died(0, outcome, 0.0, 0.0, 0.0)) is not None
