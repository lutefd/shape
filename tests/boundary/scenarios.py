"""Scenarios the boundary tests run. A separate module so spawned children can import it."""

import atexit
import sqlite3
import time

import shape


@shape.effect("t.op", scope=lambda name, **_: f"name:{name}")
def tool(name: str) -> str:
    return name


@shape.effect("t.pay", scope=lambda order, **_: f"order:{order}")
def pay(order: str) -> None:
    return None


@shape.effect("t.mail", scope=lambda order, **_: f"order:{order}", irreversible=True)
def mail(order: str) -> None:
    return None


def _marker(run: shape.Run, name: str) -> None:
    (run.dir / name).write_text("ran")


@shape.scenario(invariants=[shape.exactly_once("t.op")], max_restarts=2)
def cleanup_probe(run: shape.Run) -> None:
    """Writes to disk from `finally` and `atexit` blocks that a real crash must not run."""
    atexit.register(_marker, run, f"atexit-{run.restarts}")
    try:
        tool("a")
    finally:
        _marker(run, f"finally-{run.restarts}")


@shape.scenario(invariants=[shape.at_most_once("t.pay")], max_restarts=3)
def naive_retry(run: shape.Run) -> None:
    """Retries a charge that raised without asking whether it landed."""
    for _ in range(3):
        try:
            return pay("1")
        except (TimeoutError, ConnectionResetError):
            continue
    raise RuntimeError("gave up")


@shape.scenario(invariants=[shape.exactly_once("t.pay")], max_restarts=3)
def mark_before_effect(run: shape.Run) -> None:
    """Persists 'done' before the effect, so a crash in between loses the effect forever."""
    db = sqlite3.connect(run.dir / "progress.db")
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS done (k TEXT PRIMARY KEY)")
        fresh = db.execute("INSERT OR IGNORE INTO done VALUES ('pay')").rowcount
    if fresh:
        pay("1")


@shape.scenario(invariants=[shape.approved_before("t.mail")])
def mail_without_approval(run: shape.Run) -> None:
    mail("1")


@shape.scenario(invariants=[shape.approved_before("t.mail")])
def mail_with_approval(run: shape.Run) -> None:
    shape.approval("order:1", {"order": "1"})
    mail("1")


@shape.scenario(max_restarts=2)
def always_raises(run: shape.Run) -> None:
    raise RuntimeError("cannot recover")


@shape.scenario(max_restarts=1)
def hangs(run: shape.Run) -> None:
    time.sleep(60)


@shape.scenario(max_restarts=1)
def spawned(run: shape.Run) -> None:
    tool("s")
