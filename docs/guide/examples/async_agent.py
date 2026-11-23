"""An asyncio agent step with no framework, used by docs/guide/plain-python.md.

    shape test docs/guide/examples/async_agent.py --seeds 200

`charge_per_attempt_key` builds a new idempotency key on every attempt, so the provider cannot
tell a retry from a new charge. `charge_stable_key` derives the key from the order alone.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import shape

RETRYABLE = (TimeoutError, ConnectionResetError, shape.RateLimited, shape.ServerError)


class Payments:
    """A provider double in a sqlite file, so a charge survives the agent process dying."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS charges (key TEXT UNIQUE, order_id TEXT)")

    def _charge(self, order_id: str, key: str) -> str:
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR IGNORE INTO charges VALUES (?, ?)", (key, order_id))
        return f"ch_{key}"

    @shape.effect("payments.charge",
                  scope=lambda order_id, **_: f"order:{order_id}",
                  key=lambda idempotency_key, **_: idempotency_key)
    async def charge(self, order_id: str, amount: int, idempotency_key: str) -> str:
        return await asyncio.to_thread(self._charge, order_id, idempotency_key)


async def charge_per_attempt_key(pay: Payments, order_id: str, amount: int) -> str:
    for attempt in range(4):
        try:
            return await pay.charge(order_id, amount, idempotency_key=f"{order_id}-{attempt}")
        except RETRYABLE:
            await asyncio.sleep(0.01)
    raise RuntimeError("payment failed")


async def charge_stable_key(pay: Payments, order_id: str, amount: int) -> str:
    for _ in range(4):
        try:
            return await pay.charge(order_id, amount, idempotency_key=f"charge-{order_id}")
        except RETRYABLE:
            await asyncio.sleep(0.01)
    raise RuntimeError("payment failed")


async def handle(workdir: Path, order_id: str, charge) -> None:
    done = workdir / f"{order_id}.charged"
    if done.exists():
        return
    await charge(Payments(workdir / "payments.db"), order_id, 500)
    done.write_text("ok")


CHARGE_ONCE = [shape.at_most_once("payments.charge"), shape.exactly_once("payments.charge")]


@shape.scenario(invariants=CHARGE_ONCE, max_restarts=3, recovery="cron-retick")
def async_per_attempt_key(run: shape.Run) -> None:
    asyncio.run(handle(run.dir, "o1", charge_per_attempt_key))


@shape.scenario(invariants=CHARGE_ONCE, max_restarts=3, recovery="cron-retick")
def async_stable_key(run: shape.Run) -> None:
    asyncio.run(handle(run.dir, "o1", charge_stable_key))
