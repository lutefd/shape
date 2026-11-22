"""God's-eye external world log owned by the harness outside the agent process.

Records real committed side effects in an independent SQLite database with
synchronous=FULL so every effect that lands before a SIGKILL is preserved
regardless of what LangGraph's checkpointer believes.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class EffectRecord:
    id: int
    op: str
    order_id: str
    amount_cents: int
    idempotency_key: str | None
    pid: int
    ts: float


class WorldLog:
    """External side-effect store acting as payment processor / irreversible sink."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS effects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    op TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    idempotency_key TEXT,
                    pid INTEGER NOT NULL,
                    ts REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_effects_op_idem
                    ON effects (op, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    op TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    idempotency_key TEXT,
                    deduplicated INTEGER NOT NULL,
                    pid INTEGER NOT NULL,
                    ts REAL NOT NULL
                );
                """
            )
            conn.commit()

    def execute_effect(
        self,
        op: str,
        order_id: str,
        amount_cents: int = 0,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        """Commit an external side effect.

        When `idempotency_key` is None, every call commits a new real effect
        (non-idempotent external API). When `idempotency_key` is provided,
        duplicate calls with the same `(op, idempotency_key)` are deduplicated
        at the provider boundary (Stripe-style idempotency) and return the
        original receipt without creating a second real effect.
        """
        pid = os.getpid()
        now = time.time()
        with self._connect() as conn:
            if idempotency_key is not None:
                existing = conn.execute(
                    "SELECT id FROM effects WHERE op = ? AND idempotency_key = ?",
                    (op, idempotency_key),
                ).fetchone()
                if existing is not None:
                    conn.execute(
                        "INSERT INTO attempts (op, order_id, idempotency_key, deduplicated, pid, ts) "
                        "VALUES (?, ?, ?, 1, ?, ?)",
                        (op, order_id, idempotency_key, pid, now),
                    )
                    conn.commit()
                    return {
                        "receipt_id": f"{op}-{existing[0]}",
                        "deduplicated": True,
                    }

            cur = conn.execute(
                "INSERT INTO effects (op, order_id, amount_cents, idempotency_key, pid, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (op, order_id, amount_cents, idempotency_key, pid, now),
            )
            effect_id = cur.lastrowid
            conn.execute(
                "INSERT INTO attempts (op, order_id, idempotency_key, deduplicated, pid, ts) "
                "VALUES (?, ?, ?, 0, ?, ?)",
                (op, order_id, idempotency_key, pid, now),
            )
            conn.commit()
            return {
                "receipt_id": f"{op}-{effect_id}",
                "deduplicated": False,
            }

    def list_effects(self, op: str | None = None) -> list[EffectRecord]:
        with self._connect() as conn:
            if op is None:
                rows = conn.execute(
                    "SELECT id, op, order_id, amount_cents, idempotency_key, pid, ts "
                    "FROM effects ORDER BY id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, op, order_id, amount_cents, idempotency_key, pid, ts "
                    "FROM effects WHERE op = ? ORDER BY id",
                    (op,),
                ).fetchall()
        return [EffectRecord(*row) for row in rows]

    def count_effects(self, op: str | None = None) -> int:
        return len(self.list_effects(op))

    def count_attempts(self, op: str | None = None) -> int:
        with self._connect() as conn:
            if op is None:
                row = conn.execute("SELECT COUNT(*) FROM attempts").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM attempts WHERE op = ?", (op,)
                ).fetchone()
        return int(row[0]) if row else 0
