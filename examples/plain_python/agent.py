"""A small agent that charges a customer, with no framework: a retry loop around the payment
call, and progress saved to a sqlite file so a restarted process picks up where it left off.

`charge_with_retry` is the hand-rolled version most of us write. `charge_with_retry_fixed` is the
same loop with the one-line fix.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from payments import PaymentClient

import shape

ATTEMPTS = 4


class PaymentFailed(Exception):
    pass


def charge_with_retry(client: PaymentClient, order_id: str, amount: int) -> int:
    key = f"charge-{order_id}"
    for _ in range(ATTEMPTS):
        try:
            return client.charge(order_id, amount, idempotency_key=key)
        except (TimeoutError, shape.RateLimited, shape.ServerError):
            continue  # nothing is known to have happened: the keyed retry is safe
        except ConnectionResetError:
            # the connection dropped, so reconnect and go again
            return client.charge(order_id, amount)
    raise PaymentFailed(order_id)


def charge_with_retry_fixed(client: PaymentClient, order_id: str, amount: int) -> int:
    key = f"charge-{order_id}"
    for _ in range(ATTEMPTS):
        try:
            return client.charge(order_id, amount, idempotency_key=key)
        except (TimeoutError, ConnectionResetError, shape.RateLimited, shape.ServerError):
            continue  # the charge may have landed; the same key makes the retry safe
    raise PaymentFailed(order_id)


def process_order(workdir: Path, order_id: str, amount: int, charge) -> None:
    """Charge once and remember it. Safe to call again after a crash: it resumes."""
    progress = sqlite3.connect(workdir / "progress.db", timeout=30)
    with progress:
        progress.execute("CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, status TEXT)")
        progress.execute("INSERT OR IGNORE INTO orders VALUES (?, 'new')", (order_id,))
    (status,) = progress.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
    if status == "charged":
        return
    charge(PaymentClient(workdir / "provider.db"), order_id, amount)
    with progress:
        progress.execute("UPDATE orders SET status = 'charged' WHERE id = ?", (order_id,))
