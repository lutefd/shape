"""Sqlite-backed payments provider double for the OpenAI Agents SDK example.

No shape imports here: side effects are attached to the agent's tools from `checks.py` via
`shape.agents.effects(...)` without editing the tool or client bodies.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

ORDERS = {
    "17": {"status": "delivered", "total_cents": 4200},
    "18": {"status": "delivered", "total_cents": 900},
}


class PaymentsClient:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS refunds ("
                "id INTEGER PRIMARY KEY, order_id TEXT, amount_cents INTEGER, "
                "idempotency_key TEXT UNIQUE)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=30)

    def lookup_order(self, order_id: str) -> dict | None:
        return ORDERS.get(order_id)

    def has_refund(self, order_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM refunds WHERE order_id = ? LIMIT 1", (order_id,)
            ).fetchone()
            return row is not None

    async def refund(
        self,
        order_id: str,
        amount_cents: int,
        idempotency_key: str | None = None,
    ) -> str:
        return await asyncio.to_thread(self._refund, order_id, amount_cents, idempotency_key)

    def _refund(self, order_id: str, amount_cents: int, idempotency_key: str | None) -> str:
        with self._connect() as db:
            if idempotency_key is not None:
                row = db.execute(
                    "SELECT id FROM refunds WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row:
                    return f"re_{row[0]}"
            cursor = db.execute(
                "INSERT INTO refunds (order_id, amount_cents, idempotency_key) VALUES (?, ?, ?)",
                (order_id, amount_cents, idempotency_key),
            )
            return f"re_{cursor.lastrowid}"
