"""The payments client behind the refund tool, standing in for your real one (Stripe, Adyen, ...).

The provider is a sqlite file, so it survives the agent process dying, and it honors idempotency
keys the way real providers do: the same key returns the original refund instead of a new one.
`@shape.effect` marks the call that changes the outside world; outside a shape run it does nothing.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import shape

ORDERS = {"17": {"status": "delivered", "total_cents": 4200},
          "18": {"status": "delivered", "total_cents": 900}}


class PaymentsClient:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS refunds ("
                       "id INTEGER PRIMARY KEY, order_id TEXT, amount_cents INTEGER, "
                       "idempotency_key TEXT UNIQUE)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=30)

    def lookup_order(self, order_id: str) -> dict | None:
        return ORDERS.get(order_id)

    @shape.effect("payments.refund",
                  scope=lambda order_id, **_: f"order:{order_id}",
                  key=lambda idempotency_key=None, **_: idempotency_key)
    async def refund(self, order_id: str, amount_cents: int,
                     idempotency_key: str | None = None) -> str:
        return await asyncio.to_thread(self._refund, order_id, amount_cents, idempotency_key)

    def _refund(self, order_id: str, amount_cents: int, idempotency_key: str | None) -> str:
        with self._connect() as db:
            if idempotency_key is not None:
                row = db.execute("SELECT id FROM refunds WHERE idempotency_key = ?",
                                 (idempotency_key,)).fetchone()
                if row:
                    return f"re_{row[0]}"
            cursor = db.execute(
                "INSERT INTO refunds (order_id, amount_cents, idempotency_key) VALUES (?, ?, ?)",
                (order_id, amount_cents, idempotency_key))
            return f"re_{cursor.lastrowid}"
