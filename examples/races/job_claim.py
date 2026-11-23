"""Two workers can both process the same queued job after a check-then-act claim."""

import sqlite3
from contextlib import closing

import shape


def open_queue(run: shape.Run) -> sqlite3.Connection:
    db = sqlite3.connect(run.dir / "queue.db", autocommit=True)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, claimed INTEGER);
        INSERT OR IGNORE INTO jobs VALUES ('job-1', 0);
        CREATE TABLE IF NOT EXISTS processed (job TEXT, worker TEXT);
    """)
    return db


def claim_and_process(run: shape.Run, worker: str, *, fixed: bool) -> None:
    with closing(open_queue(run)) as db:
        if fixed:
            shape.point("claim pending job atomically")
            row = db.execute("UPDATE jobs SET claimed = 1 WHERE claimed = 0 RETURNING id").fetchone()
        else:
            shape.point("read pending job")
            row = db.execute("SELECT id FROM jobs WHERE claimed = 0").fetchone()
            if row is not None:
                shape.point("mark job claimed")
                db.execute("UPDATE jobs SET claimed = 1 WHERE id = ?", row)
        if row is not None:
            shape.point("process job")
            db.execute("INSERT INTO processed VALUES (?, ?)", (row[0], worker))


@shape.invariant
def job_processed_once(world: shape.World) -> str | None:
    with closing(open_queue(world.run)) as db:
        workers = [worker for worker, in db.execute("SELECT worker FROM processed ORDER BY worker")]
    if len(workers) != 1:
        return f"job-1 was processed {len(workers)} times by {', '.join(workers) or 'no worker'}"
    return None


def second_worker_buggy(run: shape.Run) -> None:
    claim_and_process(run, "worker-2", fixed=False)


def second_worker_fixed(run: shape.Run) -> None:
    claim_and_process(run, "worker-2", fixed=True)


@shape.scenario(concurrent=[second_worker_buggy], max_restarts=0, expect_effects=False,
                invariants=[job_processed_once])
def queue_buggy(run: shape.Run) -> None:
    claim_and_process(run, "worker-1", fixed=False)


@shape.scenario(concurrent=[second_worker_fixed], max_restarts=0, expect_effects=False,
                invariants=[job_processed_once])
def queue_fixed(run: shape.Run) -> None:
    claim_and_process(run, "worker-1", fixed=True)
