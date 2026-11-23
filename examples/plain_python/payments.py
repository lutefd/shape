"""A tiny payment provider client, standing in for your real one (Stripe, Adyen, an internal API).

The provider is a sqlite file, so it survives the agent process dying, and it honors idempotency
keys the way real providers do: the same key returns the original charge instead of a new one.
`@shape.effect` marks the call that changes the outside world. Outside a shape run it does nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import shape


class PaymentClient:
    def __init__(self, db_path: Path) -> None:
        self.db = sqlite3.connect(db_path, timeout=30)
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS charges ("
                            "id INTEGER PRIMARY KEY, order_id TEXT, amount INTEGER, "
                            "idempotency_key TEXT UNIQUE)")

    @shape.effect("payments.charge",
                  scope=lambda order_id, **_: f"order:{order_id}",
                  key=lambda idempotency_key, **_: idempotency_key)
    def charge(self, order_id: str, amount: int, idempotency_key: str | None = None) -> int:
        with self.db:
            if idempotency_key is not None:
                row = self.db.execute("SELECT id FROM charges WHERE idempotency_key = ?",
                                      (idempotency_key,)).fetchone()
                if row:
                    return row[0]
            cursor = self.db.execute(
                "INSERT INTO charges (order_id, amount, idempotency_key) VALUES (?, ?, ?)",
                (order_id, amount, idempotency_key))
            return cursor.lastrowid
