"""The leased job queue of world_fixtures.py on Postgres: every seed gets its own database cloned
from a template, and the lease is compared with the virtual clock instead of SQL now().

    export SHAPE_PG_DSN=postgresql://postgres:shape@127.0.0.1:5432/postgres
    python docs/guide/examples/postgres_queue.py        # builds the template database once
    shape test docs/guide/examples/postgres_queue.py::pg_lease_keyed --seeds 100 --jobs 4

Needs `pip install 'psycopg[binary]'` and a Postgres role with CREATEDB.
"""

from __future__ import annotations

import os

import psycopg

import shape

TEMPLATE = "shape_queue_template"
LEASE_SECONDS = 30
RETRYABLE = (TimeoutError, ConnectionResetError, shape.RateLimited, shape.ServerError,
             shape.ClientError)


def build_template(admin_dsn: str) -> None:
    """Create the template database: your migrations, run once, not once per seed."""
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEMPLATE}")
        admin.execute(f"CREATE DATABASE {TEMPLATE}")
    dsn = shape.fixtures.postgres_dsn(admin_dsn, TEMPLATE)
    with psycopg.connect(dsn) as db:
        db.execute("CREATE TABLE jobs (id text PRIMARY KEY, state text NOT NULL, "
                   "lease_until timestamptz)")


@shape.effect("queue.enqueue", scope=lambda job_id, **_: f"job:{job_id}", hide=["dsn"])
def enqueue_job(dsn: str, job_id: str) -> None:
    with psycopg.connect(dsn) as db:
        db.execute("INSERT INTO jobs VALUES (%s, 'queued', NULL)", (job_id,))


def enqueue(run: shape.Run) -> None:
    enqueue_job(run.fixture("pg"), "j1")


def claim(db: psycopg.Connection) -> str | None:
    """Reclaim jobs whose lease is older than the virtual `now`, which SQL now() would not know."""
    now = shape.clock.now()
    lease_until = now.timestamp() + LEASE_SECONDS
    row = db.execute(
        "UPDATE jobs SET state = 'leased', lease_until = to_timestamp(%s) WHERE id = ("
        "  SELECT id FROM jobs WHERE state = 'queued' OR (state = 'leased' AND lease_until < %s)"
        "  ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id", (lease_until, now)).fetchone()
    return row[0] if row else None


@shape.effect("agent.run", scope=lambda job_id: f"job:{job_id}")
def run_agent(job_id: str) -> None:
    pass


@shape.effect("agent.run", scope=lambda job_id: f"job:{job_id}", key=lambda job_id: f"run-{job_id}")
def run_agent_keyed(job_id: str) -> None:
    pass


def worker(step):
    def work(run: shape.Run) -> None:
        with psycopg.connect(run.fixture("pg"), autocommit=True) as db:
            if (job := claim(db)) is None:
                return
            for _ in range(3):
                try:
                    step(job)
                    break
                except RETRYABLE:
                    pass
            db.execute("UPDATE jobs SET state = 'done' WHERE id = %s", (job,))
    return work


PG = shape.fixtures.postgres_clone(TEMPLATE)
LEASES = shape.recovery.LeaseTakeover(lease_seconds=LEASE_SECONDS)


@shape.scenario(fixtures=[PG], setup=enqueue, recovery=LEASES,
                invariants=[shape.exactly_once("agent.run")])
def pg_lease_unkeyed(run: shape.Run) -> None:
    worker(run_agent)(run)


@shape.scenario(fixtures=[PG], setup=enqueue, recovery=LEASES,
                invariants=[shape.exactly_once("agent.run")])
def pg_lease_keyed(run: shape.Run) -> None:
    worker(run_agent_keyed)(run)


if __name__ == "__main__":
    build_template(os.environ["SHAPE_PG_DSN"])
    print(f"template database {TEMPLATE} is ready")
