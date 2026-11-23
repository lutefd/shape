"""A leased job queue, an hourly scheduler job and a lossy text message, used by
docs/guide/world-fixtures.md and docs/guide/recovery.md.

    shape test docs/guide/examples/world_fixtures.py::lease_unkeyed --seeds 100
    shape test docs/guide/examples/world_fixtures.py::lease_keyed --seeds 100

The queue lives in a SQLite file that every seed gets its own copy of (`shape.fixtures.sqlite_copy`).
A setup step enqueues the job with no faults. A worker claims it with a lease, runs the agent step
and marks it done. If the worker dies nothing renews the lease: another worker may take the job
once `shape.clock.now()` is past `lease_until`. `lease_unkeyed` runs the step without an
idempotency key, `lease_keyed` passes one.
"""

from __future__ import annotations

import sqlite3

import shape

LEASE_SECONDS = 30
# Everything the fault menu injects, so each scenario below isolates one bug.
RETRYABLE = (TimeoutError, ConnectionResetError, shape.RateLimited, shape.ServerError,
             shape.ClientError)


def schema(path: str) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT, lease_until REAL)")


@shape.effect("queue.enqueue", scope=lambda job_id, **_: f"job:{job_id}", hide=["path"])
def enqueue_job(path: str, job_id: str) -> None:
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO jobs VALUES (?, 'queued', NULL)", (job_id,))


def enqueue(run: shape.Run) -> None:
    """Setup: runs once per seed, before any worker starts, with no faults."""
    enqueue_job(run.fixture("queue"), "j1")


def claim(db: sqlite3.Connection) -> str | None:
    """Take a queued job, or one whose lease expired. The query asks the virtual clock, not SQL now()."""
    now = shape.clock.now().timestamp()
    row = db.execute(
        "UPDATE jobs SET state = 'leased', lease_until = ? WHERE id = ("
        "  SELECT id FROM jobs WHERE state = 'queued' OR (state = 'leased' AND lease_until < ?)"
        "  LIMIT 1) RETURNING id", (now + LEASE_SECONDS, now)).fetchone()
    return row[0] if row else None


@shape.effect("agent.run", scope=lambda job_id: f"job:{job_id}")
def run_agent(job_id: str) -> None:
    pass


@shape.effect("agent.run", scope=lambda job_id: f"job:{job_id}",
              key=lambda job_id: f"run-{job_id}")
def run_agent_keyed(job_id: str) -> None:
    pass


def worker(step):
    def work(run: shape.Run) -> None:
        with sqlite3.connect(run.fixture("queue"), isolation_level=None) as db:
            if (job := claim(db)) is None:
                return
            for _ in range(3):
                try:
                    step(job)
                    break
                except RETRYABLE:
                    pass
            db.execute("UPDATE jobs SET state = 'done' WHERE id = ?", (job,))
    return work


QUEUE = shape.fixtures.sqlite_copy(init=schema, name="queue")
LEASES = shape.recovery.LeaseTakeover(lease_seconds=LEASE_SECONDS)
ONCE = [shape.exactly_once("agent.run")]


@shape.scenario(fixtures=[QUEUE], setup=enqueue, recovery=LEASES, invariants=ONCE)
def lease_unkeyed(run: shape.Run) -> None:
    worker(run_agent)(run)


@shape.scenario(fixtures=[QUEUE], setup=enqueue, recovery=LEASES, invariants=ONCE)
def lease_keyed(run: shape.Run) -> None:
    worker(run_agent_keyed)(run)


# ------------------------------------------------------------------------------------ scheduler


def current_hour() -> int:
    return int((shape.clock.now() - shape.clock.EPOCH).total_seconds() // 3600)


@shape.effect("digest.send", scope=lambda hour: f"hour:{hour}", key=lambda hour: f"digest-{hour}")
def send_digest(hour: int) -> None:
    pass


@shape.invariant
def every_hour_has_a_digest(world: shape.World) -> str | None:
    last = int((world.run.now() - shape.clock.EPOCH).total_seconds() // 3600)
    sent = {e["args"]["hour"] for e in world.effects("digest.send")}
    missing = [hour for hour in range(last + 1) if hour not in sent]
    return f"no digest was sent for hour {missing[0]}" if missing else None


HOURLY = shape.recovery.APSchedulerReload(interval=3600)


@shape.scenario(max_restarts=2, recovery=HOURLY, invariants=[every_hour_has_a_digest])
def digest_current_hour(run: shape.Run) -> None:
    """Sends the digest for the hour it was started in. A tick that dies is not rerun."""
    send_digest(current_hour())


@shape.scenario(max_restarts=2, recovery=HOURLY, invariants=[every_hour_has_a_digest])
def digest_catches_up(run: shape.Run) -> None:
    """Sends every digest since the last one it recorded, so a lost tick is made up at the next."""
    marker = run.dir / "last_hour"
    last = int(marker.read_text()) if marker.exists() else -1
    for hour in range(last + 1, current_hour() + 1):
        send_digest(hour)
        marker.write_text(str(hour))


# ------------------------------------------------------------------------------ at-most-once


@shape.effect("sms.send", scope=lambda user: f"user:{user}", delivery="at-most-once")
def send_sms(user: str) -> None:
    pass


@shape.effect("sms.send", scope=lambda user: f"user:{user}", key=lambda user: f"sms-{user}",
              delivery="at-most-once")
def send_sms_keyed(user: str) -> None:
    pass


def fire_and_forget(send):
    def job(run: shape.Run) -> None:
        try:
            send("u1")
        except (shape.ServerError, shape.RateLimited, shape.ClientError, TimeoutError,
                ConnectionError):
            pass  # nobody retries a text message
    return job


@shape.scenario(max_restarts=1, recovery="redeliver", invariants=[shape.exactly_once("sms.send")])
def sms_unkeyed(run: shape.Run) -> None:
    fire_and_forget(send_sms)(run)


@shape.scenario(max_restarts=1, recovery="redeliver", invariants=[shape.exactly_once("sms.send")])
def sms_keyed(run: shape.Run) -> None:
    fire_and_forget(send_sms_keyed)(run)
