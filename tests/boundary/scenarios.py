"""Scenarios the boundary tests run. A separate module so spawned children can import it."""

import asyncio
import atexit
import sqlite3
import time
from pathlib import Path

import shape


@shape.effect("t.op", scope=lambda name, **_: f"name:{name}")
def tool(name: str) -> str:
    return name


@shape.effect("t.pay", scope=lambda order, **_: f"order:{order}")
def pay(order: str) -> None:
    return None


@shape.effect("t.apay", scope=lambda order, **_: f"order:{order}")
async def apay(order: str, marker: str | None = None) -> str:
    await asyncio.sleep(0)
    if marker:  # the body's real side effect, visible even if the process dies right after
        Path(marker).write_text("body ran\n")
    return order


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


@shape.scenario(invariants=[shape.exactly_once("t.apay")], max_restarts=2)
def async_tool(run: shape.Run) -> None:
    """An `async def` effect awaited from an event loop, with progress saved only after it returns."""
    if run.restarts == 0:
        asyncio.run(apay("1", str(run.dir / "apay-body")))


@shape.scenario(invariants=[], max_restarts=0)
def async_never_awaited(run: shape.Run) -> None:
    """Creates the coroutine and drops it: nothing ran, so nothing may be recorded or faulted."""
    coro = apay("1", str(run.dir / "apay-body"))
    coro.close()


@shape.effect("t.refund", scope=lambda order, **_: f"order:{order}")
def refund(order: str) -> None:
    pass


@shape.scenario(invariants=[shape.at_most_once("t.refund")], max_restarts=0)
def model_may_repeat_a_refund(run: shape.Run) -> None:
    refund("1")
    if shape.choose("after_refund", ["reply_to_customer", "refund_again"]) == "refund_again":
        refund("1")


@shape.effect("t.keyed_refund", scope=lambda order, **_: f"order:{order}",
              key=lambda order, **_: f"refund-{order}")
def keyed_refund(order: str) -> None:
    pass


@shape.scenario(invariants=[shape.at_most_once("t.keyed_refund")])
def model_never_repeats(run: shape.Run) -> None:
    for _ in range(3):
        try:
            keyed_refund("1")
            break
        except (TimeoutError, ConnectionResetError, shape.RateLimited, shape.ServerError):
            continue
    shape.choose("after_refund", ["reply_to_customer", "ask_a_question", "close_ticket"])
