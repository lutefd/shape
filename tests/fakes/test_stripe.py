"""Tests for `StripeFake` semantics, idempotency, errors, state persistence, and invariants."""

from __future__ import annotations

import json
from typing import Any

from shape.boundary.invariants import World
from shape.fakes import HttpFake, Request, StripeFake


def _roundtrip(state: dict) -> dict:
    return json.loads(json.dumps(state))


def _req(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> Request:
    hdrs = dict(headers or {})
    if payload is not None and not body:
        body = json.dumps(payload).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    return Request(
        method=method,
        url=f"https://api.stripe.com{path}",
        headers=hdrs,
        body=body,
        json=payload,
    )


def test_stripe_protocol_and_customers():
    fake = StripeFake()
    assert isinstance(fake, HttpFake)
    assert fake.hosts == ("api.stripe.com",)
    assert fake.api_version == "2024-06-20"

    state: dict = {}
    req = _req("POST", "/v1/customers", payload={"email": "ada@example.com", "name": "Ada"})
    resp = fake.handle(req, state)
    assert resp.status == 200
    cus = json.loads(resp.body)
    assert cus["id"] == "cus_1"
    assert cus["object"] == "customer"
    assert cus["email"] == "ada@example.com"
    eff = fake.effect(req, resp)
    assert eff is not None and eff.op == "stripe.customer.create" and eff.landed is True

    state = _roundtrip(state)
    get_req = _req("GET", "/v1/customers/cus_1")
    get_resp = fake.handle(get_req, state)
    assert get_resp.status == 200
    assert json.loads(get_resp.body)["email"] == "ada@example.com"
    assert fake.effect(get_req, get_resp) is None
    assert fake.describe(get_req) is None

    miss_resp = fake.handle(_req("GET", "/v1/customers/cus_999"), state)
    assert miss_resp.status == 404
    err = json.loads(miss_resp.body)["error"]
    assert err["type"] == "invalid_request_error"
    assert err["code"] == "resource_missing"


def test_stripe_payment_intent_lifecycle_and_form_encoding():
    fake = StripeFake()
    state: dict = {}

    # Form-encoded body with nested metadata[order_id]
    create_req = _req(
        "POST",
        "/v1/payment_intents",
        body=b"amount=2500&currency=USD&metadata[order_id]=ord-7",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = fake.handle(create_req, state)
    assert resp.status == 200
    pi = json.loads(resp.body)
    assert pi["id"] == "pi_1"
    assert pi["status"] == "requires_payment_method"
    assert pi["currency"] == "usd"
    assert pi["metadata"] == {"order_id": "ord-7"}
    eff = fake.effect(create_req, resp)
    assert eff is not None and eff.op == "stripe.payment_intent.create"
    assert eff.scope == "order:ord-7" and eff.landed is True

    state = _roundtrip(state)

    # Decline on confirm -> 402 card_error, landed=False
    bad_confirm = _req(
        "POST",
        "/v1/payment_intents/pi_1/confirm",
        payload={"payment_method": "pm_card_chargeDeclined"},
    )
    bad_resp = fake.handle(bad_confirm, state)
    assert bad_resp.status == 402
    assert json.loads(bad_resp.body)["error"]["type"] == "card_error"
    assert json.loads(bad_resp.body)["error"]["code"] == "card_declined"
    bad_eff = fake.effect(bad_confirm, bad_resp)
    assert bad_eff is not None and bad_eff.op == "stripe.charge" and bad_eff.landed is False

    # Valid confirm -> 200, creates charge ch_1, scope inherited from PI metadata
    confirm_req = _req(
        "POST",
        "/v1/payment_intents/pi_1/confirm",
        payload={"payment_method": "pm_card_visa"},
    )
    confirm_resp = fake.handle(confirm_req, state)
    assert confirm_resp.status == 200
    confirmed = json.loads(confirm_resp.body)
    assert confirmed["status"] == "succeeded"
    assert confirmed["amount_received"] == 2500
    assert confirmed["latest_charge"] == "ch_1"
    confirm_eff = fake.effect(confirm_req, confirm_resp)
    assert confirm_eff is not None and confirm_eff.op == "stripe.charge"
    assert confirm_eff.scope == "order:ord-7" and confirm_eff.landed is True

    state = _roundtrip(state)

    # Confirming again fails with 400 payment_intent_unexpected_state and landed=False
    again_resp = fake.handle(confirm_req, state)
    assert again_resp.status == 400
    assert json.loads(again_resp.body)["error"]["code"] == "payment_intent_unexpected_state"
    assert fake.effect(confirm_req, again_resp).landed is False

    # Canceling a succeeded PI also fails
    cancel_req = _req("POST", "/v1/payment_intents/pi_1/cancel")
    cancel_resp = fake.handle(cancel_req, state)
    assert cancel_resp.status == 400
    assert fake.effect(cancel_req, cancel_resp).landed is False


def test_stripe_refunds_partial_over_refund_and_double_refund():
    fake = StripeFake()
    state: dict = {}

    ch_req = _req(
        "POST",
        "/v1/charges",
        payload={"amount": 1000, "currency": "usd", "metadata": {"order_id": "42"}},
    )
    ch_resp = fake.handle(ch_req, state)
    assert ch_resp.status == 200
    ch_id = json.loads(ch_resp.body)["id"]
    state = _roundtrip(state)

    # Partial refund of 400 succeeds
    r1_req = _req("POST", "/v1/refunds", payload={"charge": ch_id, "amount": 400})
    r1_resp = fake.handle(r1_req, state)
    assert r1_resp.status == 200
    assert json.loads(r1_resp.body)["amount"] == 400
    r1_eff = fake.effect(r1_req, r1_resp)
    assert r1_eff is not None and r1_eff.op == "stripe.refund"
    assert r1_eff.irreversible is True and r1_eff.landed is True
    state = _roundtrip(state)

    # Refund > remaining (700 > 600) fails with 400 amount_too_large and landed=False
    over_req = _req("POST", "/v1/refunds", payload={"charge": ch_id, "amount": 700})
    over_resp = fake.handle(over_req, state)
    assert over_resp.status == 400
    assert json.loads(over_resp.body)["error"]["code"] == "amount_too_large"
    assert fake.effect(over_req, over_resp).landed is False

    # Refund remaining (omitting amount defaults to remaining 600)
    r2_req = _req("POST", "/v1/refunds", payload={"charge": ch_id})
    r2_resp = fake.handle(r2_req, state)
    assert r2_resp.status == 200
    assert json.loads(r2_resp.body)["amount"] == 600
    state = _roundtrip(state)

    # Refunding twice fails with 400 charge_already_refunded and landed=False
    dup_req = _req("POST", "/v1/refunds", payload={"charge": ch_id})
    dup_resp = fake.handle(dup_req, state)
    assert dup_resp.status == 400
    assert json.loads(dup_resp.body)["error"]["code"] == "charge_already_refunded"
    assert fake.effect(dup_req, dup_resp).landed is False


def test_stripe_idempotency_replay_mismatch_errors_and_24h_expiry():
    now = [1_700_000_000.0]
    fake = StripeFake(clock=lambda: now[0])
    state: dict = {}

    # 1. Same key + same params -> replays original response without creating a 2nd charge
    req1 = _req(
        "POST",
        "/v1/charges",
        payload={"amount": 500, "currency": "usd", "metadata": {"order_id": "1"}},
        headers={"Idempotency-Key": "idem-charge-1"},
    )
    resp1 = fake.handle(req1, state)
    assert resp1.status == 200
    assert "Idempotent-Replayed" not in resp1.headers
    state = _roundtrip(state)

    now[0] += 86399.0  # 1 second before 24h expiry
    resp1_replay = fake.handle(req1, state)
    assert resp1_replay.status == 200
    assert resp1_replay.headers["Idempotent-Replayed"] == "true"
    assert json.loads(resp1_replay.body)["id"] == json.loads(resp1.body)["id"]
    assert len(state["charges"]) == 1

    # 2. Same key + different params -> 400 idempotency_error, landed=False, original preserved
    mismatch = _req(
        "POST",
        "/v1/charges",
        payload={"amount": 999, "currency": "usd", "metadata": {"order_id": "1"}},
        headers={"Idempotency-Key": "idem-charge-1"},
    )
    mis_resp = fake.handle(mismatch, state)
    assert mis_resp.status == 400
    assert json.loads(mis_resp.body)["error"]["type"] == "idempotency_error"
    assert fake.effect(mismatch, mis_resp).landed is False
    # Original entry still replays identically
    assert json.loads(fake.handle(req1, state).body)["id"] == "ch_1"

    # 3. Same key + same params replays original ERROR response (402 card_declined)
    decline_req = _req(
        "POST",
        "/v1/charges",
        payload={"amount": 500, "currency": "usd", "source": "tok_chargeDeclined"},
        headers={"Idempotency-Key": "idem-err-1"},
    )
    err1 = fake.handle(decline_req, state)
    assert err1.status == 402
    assert fake.effect(decline_req, err1).landed is False
    state = _roundtrip(state)
    err2 = fake.handle(decline_req, state)
    assert err2.status == 402
    assert err2.headers["Idempotent-Replayed"] == "true"
    assert err2.body == err1.body
    assert fake.effect(decline_req, err2).landed is False

    # 4. At 24h expiry, key expires and executes fresh
    now[0] += 2.0  # total 86401s >= 86400s
    resp_after_expiry = fake.handle(req1, state)
    assert resp_after_expiry.status == 200
    assert "Idempotent-Replayed" not in resp_after_expiry.headers
    assert json.loads(resp_after_expiry.body)["id"] == "ch_2"
    assert len(state["charges"]) == 2

    # 5. Without clock, keys never expire
    no_clock = StripeFake()
    s2: dict = {}
    r_a = no_clock.handle(req1, s2)
    r_b = no_clock.handle(req1, _roundtrip(s2))
    assert json.loads(r_a.body)["id"] == json.loads(r_b.body)["id"]
    assert r_b.headers["Idempotent-Replayed"] == "true"

    # 6. Overly long key (> 255 chars) -> 400 idempotency_error
    long_req = _req(
        "POST",
        "/v1/charges",
        payload={"amount": 100, "currency": "usd"},
        headers={"Idempotency-Key": "k" * 256},
    )
    long_resp = fake.handle(long_req, state)
    assert long_resp.status == 400
    assert json.loads(long_resp.body)["error"]["type"] == "idempotency_error"
    assert fake.effect(long_req, long_resp).landed is False


def test_stripe_default_invariants():
    fake = StripeFake(scope_metadata_key="order_id")
    invs = {inv.name: inv for inv in fake.invariants()}
    assert "at_most_once(stripe.charge)" in invs
    assert "at_most_once(stripe.refund)" in invs
    assert "stripe_refunds_le_charges" in invs

    # Same idempotency key dedupes to 1 charge -> passes
    w_ok = World(
        entries=[
            {"kind": "effect", "op": "stripe.charge", "scope": "order_id:1", "key": "k1",
             "result": {"id": "ch_1", "object": "charge", "amount": 500}, "args": {}},
            {"kind": "effect", "op": "stripe.charge", "scope": "order_id:1", "key": "k1",
             "result": {"id": "ch_1", "object": "charge", "amount": 500}, "args": {}},
            {"kind": "effect", "op": "stripe.refund", "scope": "order_id:1", "key": "r1",
             "result": {"id": "re_1", "charge": "ch_1", "amount": 500}, "args": {}},
        ],
        completed=True,
        incarnations=1,
        max_restarts=1,
    )
    for inv in invs.values():
        assert inv.check(w_ok) is None

    # Double charge without key violates at_most_once
    w_dup = World(
        entries=[
            {"kind": "effect", "op": "stripe.charge", "scope": "order_id:1", "key": None,
             "result": {"amount": 500}, "args": {}},
            {"kind": "effect", "op": "stripe.charge", "scope": "order_id:1", "key": None,
             "result": {"amount": 500}, "args": {}},
        ],
        completed=True,
        incarnations=1,
        max_restarts=1,
    )
    assert invs["at_most_once(stripe.charge)"].check(w_dup) is not None

    # Refund exceeding charge violates stripe_refunds_le_charges
    w_over = World(
        entries=[
            {"kind": "effect", "op": "stripe.charge", "scope": "order_id:1", "key": "k1",
             "result": {"id": "ch_1", "object": "charge", "amount": 500}, "args": {}},
            {"kind": "effect", "op": "stripe.refund", "scope": "order_id:1", "key": "r1",
             "result": {"id": "re_1", "charge": "ch_1", "amount": 600}, "args": {}},
        ],
        completed=True,
        incarnations=1,
        max_restarts=1,
    )
    assert invs["stripe_refunds_le_charges"].check(w_over) is not None
