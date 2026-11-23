"""Sqlite-backed external provider doubles and HTTP error factories for the LangGraph examples.

Each provider writes to a sqlite file inside `run.dir`, so committed effects survive real
process crashes (`os._exit(137)`). Providers honor idempotency keys the way real payment/order
APIs do: repeating a call with the same non-null key returns the original record without
creating a second effect.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import httpx
import requests


def httpx_read_timeout() -> httpx.ReadTimeout:
    return httpx.ReadTimeout("timed out waiting for payment API response")


def requests_http_error(status: int, reason: str) -> Callable[[], requests.HTTPError]:
    def factory() -> requests.HTTPError:
        resp = requests.Response()
        resp.status_code = status
        resp.reason = reason
        resp.url = "https://payments.example/v1/charges"
        return requests.HTTPError(f"{status} {reason}", response=resp)

    return factory


def httpx_status_error(status: int, reason: str) -> Callable[[], httpx.HTTPStatusError]:
    def factory() -> httpx.HTTPStatusError:
        req = httpx.Request("POST", "https://payments.example/v1/charges")
        resp = httpx.Response(status, request=req)
        return httpx.HTTPStatusError(f"{status} {reason}", request=req, response=resp)

    return factory


class PaymentProvider:
    def __init__(self, db_path: Path) -> None:
        self.db = sqlite3.connect(db_path, timeout=30)
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS charges ("
                "id INTEGER PRIMARY KEY, order_id TEXT, amount INTEGER, "
                "idempotency_key TEXT UNIQUE)"
            )

    def charge(self, order_id: str, amount: int, idempotency_key: str | None = None) -> int:
        with self.db:
            if idempotency_key is not None:
                row = self.db.execute(
                    "SELECT id FROM charges WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row:
                    return row[0]
            cursor = self.db.execute(
                "INSERT INTO charges (order_id, amount, idempotency_key) VALUES (?, ?, ?)",
                (order_id, amount, idempotency_key),
            )
            return int(cursor.lastrowid)

    def close(self) -> None:
        self.db.close()


class OrderProvider:
    def __init__(self, db_path: Path) -> None:
        self.db = sqlite3.connect(db_path, timeout=30)
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS confirmations ("
                "id INTEGER PRIMARY KEY, order_id TEXT, charge_id INTEGER, "
                "idempotency_key TEXT UNIQUE)"
            )

    def confirm(
        self,
        order_id: str,
        charge_id: int,
        idempotency_key: str | None = None,
    ) -> int:
        with self.db:
            if idempotency_key is not None:
                row = self.db.execute(
                    "SELECT id FROM confirmations WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row:
                    return row[0]
            cursor = self.db.execute(
                "INSERT INTO confirmations (order_id, charge_id, idempotency_key) "
                "VALUES (?, ?, ?)",
                (order_id, charge_id, idempotency_key),
            )
            return int(cursor.lastrowid)

    def close(self) -> None:
        self.db.close()


class TransferProvider:
    def __init__(self, db_path: Path) -> None:
        self.db = sqlite3.connect(db_path, timeout=30)
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS transfers ("
                "id INTEGER PRIMARY KEY, order_id TEXT, recipient TEXT, amount INTEGER, "
                "idempotency_key TEXT UNIQUE)"
            )

    def send(
        self,
        order_id: str,
        to: str,
        amount: int,
        idempotency_key: str | None = None,
    ) -> int:
        with self.db:
            if idempotency_key is not None:
                row = self.db.execute(
                    "SELECT id FROM transfers WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row:
                    return row[0]
            cursor = self.db.execute(
                "INSERT INTO transfers (order_id, recipient, amount, idempotency_key) "
                "VALUES (?, ?, ?, ?)",
                (order_id, to, amount, idempotency_key),
            )
            return int(cursor.lastrowid)

    def close(self) -> None:
        self.db.close()


ORDERS = {
    "17": {"status": "delivered", "total_cents": 4200},
    "18": {"status": "delivered", "total_cents": 900},
}


class RefundProvider:
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

    def lookup(self, order_id: str) -> dict | None:
        return ORDERS.get(order_id)

    def refund(
        self,
        order_id: str,
        amount_cents: int,
        idempotency_key: str | None = None,
    ) -> str:
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

    def close(self) -> None:
        pass
