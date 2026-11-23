"""One small buggy scenario per built-in invariant, used by docs/guide/invariants.md.

    shape test docs/guide/examples/invariants.py --seeds 50

Every scenario here fails on purpose. The effects are stubs: shape records what they did in its
own world log, so they need no provider behind them.
"""

from __future__ import annotations

import contextlib
import sqlite3

import shape


@shape.effect("payments.charge", scope=lambda order, **_: f"order:{order}",
              key=lambda key=None, **_: key)
def charge(order: str, amount: int, key: str | None = None) -> str:
    return f"ch_{order}"


@shape.effect("payments.refund", scope=lambda order, **_: f"order:{order}",
              result=lambda reply: reply)
def refund(order: str, amount: int) -> dict:
    return {"refunded": amount if amount < 1000 else 999}


@shape.effect("email.send", scope=lambda order, **_: f"order:{order}", irreversible=True)
def send_receipt(order: str, text: str) -> None:
    pass


@shape.effect("transfers.send", scope=lambda order, **_: f"order:{order}", irreversible=True)
def wire(order: str, amount: int) -> None:
    pass


# at_most_once: a retry that drops the idempotency key.
@shape.scenario(invariants=[shape.at_most_once("payments.charge")], max_restarts=1)
def retry_without_key(run: shape.Run) -> None:
    try:
        charge("1", 500, key="order-1")
    except (TimeoutError, ConnectionResetError):
        charge("1", 500)  # the first call may have landed; this one has no key


# exactly_once: progress is saved before the effect, so a crash in between loses it.
@shape.scenario(invariants=[shape.exactly_once("payments.charge")], max_restarts=3)
def mark_done_first(run: shape.Run) -> None:
    db = sqlite3.connect(run.dir / "progress.db")
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS done (step TEXT PRIMARY KEY)")
        fresh = db.execute("INSERT OR IGNORE INTO done VALUES ('charge')").rowcount
    if fresh:
        charge("1", 500, key="order-1")


# approved_before: the human approved 100, the model sends 1000.
@shape.scenario(invariants=[shape.approved_before("transfers.send")], max_restarts=1)
def wire_other_amount(run: shape.Run) -> None:
    shape.approval("order:1", {"order": "1", "amount": 100})
    wire("1", shape.choose("amount", [100, 1000]))


# requires: the receipt goes out even when the charge never landed.
@shape.scenario(invariants=[shape.requires("email.send", before="payments.charge")],
                max_restarts=1)
def receipt_after_failed_charge(run: shape.Run) -> None:
    with contextlib.suppress(Exception):
        charge("1", 500, key="order-1")
    send_receipt("1", "You were charged 500.")


# within_scopes: the model refunds an order the ticket is not about.
@shape.scenario(invariants=[shape.within_scopes("payments.refund", {"order:1"})],
                max_restarts=1)
def refund_wrong_order(run: shape.Run) -> None:
    refund(shape.choose("which_order", ["1", "2"]), 500)


# max_calls: a retry loop with no budget hammers the provider.
@shape.scenario(invariants=[shape.max_calls("payments.charge", 3)], max_restarts=1)
def retry_storm(run: shape.Run) -> None:
    for _ in range(10):
        with contextlib.suppress(Exception):
            return charge("1", 500, key="order-1")


# sum_le: two partial refunds add up to more than the order total.
@shape.scenario(invariants=[shape.sum_le("payments.refund", "amount", 500)], max_restarts=1)
def over_refund(run: shape.Run) -> None:
    refund("1", 500)
    if shape.choose("after_refund", ["reply", "refund_shipping_too"]) == "refund_shipping_too":
        refund("1", 50)


# claims_true: the agent tells the user "refunded" whether or not the refund landed.
@shape.scenario(invariants=[shape.claims_true()], max_restarts=1)
def claim_without_refund(run: shape.Run) -> None:
    with contextlib.suppress(Exception):
        refund("1", 500)
    shape.claim("order:1", "payments.refund")


# A custom invariant over shape.World: the provider refunded less than was asked.
@shape.invariant
def refund_matches_request(world: shape.World) -> str | None:
    for e in world.effects("payments.refund"):
        if e["result"]["refunded"] != e["args"]["amount"]:
            return f"asked to refund {e['args']['amount']}, provider refunded {e['result']['refunded']}"
    return None


@shape.scenario(invariants=[refund_matches_request], max_restarts=1)
def partial_refund(run: shape.Run) -> None:
    refund("1", shape.choose("amount", [500, 1500]))
