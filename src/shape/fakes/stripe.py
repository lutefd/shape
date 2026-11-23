"""Stripe REST API test double (`api.stripe.com`).

Mirrors Stripe API version `2024-06-20`:
- PaymentIntents (`POST /v1/payment_intents`, `/confirm`, `/cancel`, `GET`):
  https://docs.stripe.com/api/payment_intents
- Charges (`POST/GET /v1/charges`): https://docs.stripe.com/api/charges
- Refunds (`POST/GET /v1/refunds`): https://docs.stripe.com/api/refunds
- Customers (`POST/GET /v1/customers`): https://docs.stripe.com/api/customers
- Idempotent requests (`Idempotency-Key` on POST):
  https://docs.stripe.com/api/idempotent_requests
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from ..boundary.invariants import Invariant, World, at_most_once
from ._util import (
    header,
    json_response,
    next_seq,
    parse_bool,
    parse_int,
    parse_params,
    parse_url,
    response_json,
)
from .base import EffectInfo, Request, Response

_PI_ITEM_RE = re.compile(r"^/v1/payment_intents/([^/]+)$")
_PI_CONFIRM_RE = re.compile(r"^/v1/payment_intents/([^/]+)/confirm$")
_PI_CANCEL_RE = re.compile(r"^/v1/payment_intents/([^/]+)/cancel$")
_CHARGE_ITEM_RE = re.compile(r"^/v1/charges/([^/]+)$")
_REFUND_ITEM_RE = re.compile(r"^/v1/refunds/([^/]+)$")
_CUST_ITEM_RE = re.compile(r"^/v1/customers/([^/]+)$")

_IDEMPOTENCY_TTL_SECONDS = 86400.0
_MAX_IDEMPOTENCY_KEY_LEN = 255
_BASE_TS = 1_700_000_000


def _err(
    status: int, err_type: str, message: str, *,
    code: str | None = None, param: str | None = None, decline_code: str | None = None,
) -> Response:
    body: dict[str, Any] = {"type": err_type, "message": message}
    for k, v in (("code", code), ("param", param), ("decline_code", decline_code)):
        if v is not None:
            body[k] = v
    return json_response(status, {"error": body})


class StripeFake:
    """Faithful test double for Stripe PaymentIntents, Charges, Refunds, and Customers."""

    hosts: tuple[str, ...] = ("api.stripe.com",)
    api_version: str = "2024-06-20"

    def __init__(
        self, *,
        scope: Callable[[Request, dict[str, Any]], str] | None = None,
        scope_metadata_key: str | None = None,
        clock: Callable[[], float] | None = None,
        idempotency_ttl: float = _IDEMPOTENCY_TTL_SECONDS,
        initial_charges: Mapping[str, int] | None = None,
        hosts: tuple[str, ...] | None = None,
    ) -> None:
        self._scope_fn = scope
        self._scope_metadata_key = scope_metadata_key
        self._clock = clock
        self._idempotency_ttl = idempotency_ttl
        self._initial_charges = dict(initial_charges or {})
        if hosts is not None:
            self.hosts = tuple(hosts)

    def _now(self) -> int:
        return int(self._clock()) if self._clock is not None else _BASE_TS

    def _scope(
        self, req: Request, params: dict[str, Any], body: dict[str, Any] | None = None
    ) -> tuple[str, str, str | None]:
        if self._scope_fn is not None:
            return self._scope_fn(req, params), "explicit", None
        meta = params.get("metadata")
        if not isinstance(meta, dict) and isinstance(body, dict):
            meta = body.get("metadata")
        if isinstance(meta, dict):
            if self._scope_metadata_key and self._scope_metadata_key in meta:
                return (
                    f"{self._scope_metadata_key}:{meta[self._scope_metadata_key]}",
                    "explicit",
                    f"scope_metadata_key={self._scope_metadata_key!r}",
                )
            if "order_id" in meta:
                return f"order:{meta['order_id']}", "fake_default", "metadata.order_id"
            if "scope" in meta:
                return str(meta["scope"]), "fake_default", "metadata.scope"
            if meta:
                k = min(meta)
                return f"{k}:{meta[k]}", "fake_default", f"metadata.{k}"
        path, _ = parse_url(req.url)
        if m := _PI_CONFIRM_RE.match(path) or _PI_CANCEL_RE.match(path):
            return f"payment_intent:{m.group(1)}", "fake_default", "payment_intent"
        for field in ("charge", "payment_intent", "customer"):
            val = params.get(field) or (body.get(field) if isinstance(body, dict) else None)
            if val:
                return f"{field}:{val}", "fake_default", field
        return "", "fake_default", "empty"

    def _route_op(self, method: str, path: str, params: dict[str, Any]) -> tuple[str, bool] | None:
        if method != "POST":
            return None
        if path == "/v1/charges" or _PI_CONFIRM_RE.match(path):
            return ("stripe.charge", False)
        if path == "/v1/payment_intents":
            return ("stripe.charge", False) if parse_bool(params.get("confirm")) else (
                "stripe.payment_intent.create", False
            )
        if _PI_CANCEL_RE.match(path):
            return ("stripe.payment_intent.cancel", False)
        if path == "/v1/refunds":
            return ("stripe.refund", True)
        if path == "/v1/customers":
            return ("stripe.customer.create", False)
        return None

    def describe(self, req: Request) -> EffectInfo | None:
        path, _ = parse_url(req.url)
        params = parse_params(req)
        routed = self._route_op(req.method.upper(), path, params)
        if routed is None:
            return None
        op, irreversible = routed
        scope, scope_source, scope_rule = self._scope(req, params)
        return EffectInfo(op, scope, header(req.headers, "Idempotency-Key"),
                          irreversible, False, scope_source, scope_rule)

    def effect(self, req: Request, resp: Response) -> EffectInfo | None:
        path, _ = parse_url(req.url)
        params = parse_params(req)
        routed = self._route_op(req.method.upper(), path, params)
        if routed is None:
            return None
        op, irreversible = routed
        scope, scope_source, scope_rule = self._scope(req, params, response_json(resp))
        return EffectInfo(op, scope, header(req.headers, "Idempotency-Key"),
                          irreversible, 200 <= resp.status < 300, scope_source, scope_rule)

    def handle(self, req: Request, state: dict) -> Response:
        for k in ("customers", "payment_intents", "charges", "refunds", "idempotency"):
            state.setdefault(k, {})
        method = req.method.upper()
        path, _ = parse_url(req.url)
        params = parse_params(req)

        if method == "POST" and (idem_key := header(req.headers, "Idempotency-Key")) is not None:
            if len(idem_key) > _MAX_IDEMPOTENCY_KEY_LEN:
                return _err(400, "idempotency_error",
                            f"Idempotency-Key must be at most {_MAX_IDEMPOTENCY_KEY_LEN} characters.")
            fp = json.dumps([method, path, params], sort_keys=True, separators=(",", ":"))
            saved = state["idempotency"].get(idem_key)
            if saved is not None:
                expired = (self._clock is not None and saved.get("created_at") is not None
                           and (self._clock() - float(saved["created_at"])) >= self._idempotency_ttl)
                if expired:
                    del state["idempotency"][idem_key]
                elif saved["fingerprint"] != fp:
                    return _err(400, "idempotency_error",
                                f"Keys for idempotent requests can only be used with the same "
                                f"parameters they were first used with. Try using a key other "
                                f"than '{idem_key}' if you meant to execute a different request.")
                else:
                    hdrs = {**saved["headers"], "Idempotent-Replayed": "true"}
                    return Response(int(saved["status"]), hdrs, saved["body"].encode("utf-8"))
            resp = self._dispatch(method, path, params, state)
            state["idempotency"][idem_key] = {
                "fingerprint": fp, "status": resp.status, "headers": dict(resp.headers),
                "body": resp.body.decode("utf-8"),
                "created_at": self._clock() if self._clock is not None else None,
            }
            return resp
        return self._dispatch(method, path, params, state)

    def _dispatch(self, method: str, path: str, params: dict[str, Any], state: dict) -> Response:
        if method == "GET":
            for col, rx, store_k, kind in (
                ("/v1/customers", _CUST_ITEM_RE, "customers", "customer"),
                ("/v1/payment_intents", _PI_ITEM_RE, "payment_intents", "payment_intent"),
                ("/v1/charges", _CHARGE_ITEM_RE, "charges", "charge"),
                ("/v1/refunds", _REFUND_ITEM_RE, "refunds", "refund"),
            ):
                if path == col:
                    return json_response(200, {"object": "list", "data": list(state[store_k].values()),
                                               "has_more": False})
                if m := rx.match(path):
                    item = state[store_k].get(m.group(1))
                    if item is None:
                        return _err(404, "invalid_request_error",
                                    f"No such {kind}: '{m.group(1)}'", code="resource_missing")
                    return json_response(200, item)
        elif method == "POST":
            if path == "/v1/customers":
                return self._create_customer(params, state)
            if path == "/v1/payment_intents":
                return self._create_payment_intent(params, state)
            if m := _PI_CONFIRM_RE.match(path):
                return self._confirm_payment_intent(m.group(1), params, state)
            if m := _PI_CANCEL_RE.match(path):
                return self._cancel_payment_intent(m.group(1), state)
            if path == "/v1/charges":
                return self._create_charge(params, state)
            if path == "/v1/refunds":
                return self._create_refund(params, state)
        return _err(404, "invalid_request_error", f"Unrecognized request URL ({method}: {path}).")

    def _create_customer(self, params: dict[str, Any], state: dict) -> Response:
        cid = f"cus_{next_seq(state, '_cus_seq')}"
        cust = {
            "id": cid, "object": "customer", "created": self._now(),
            "email": params.get("email"), "name": params.get("name"),
            "description": params.get("description"), "metadata": dict(params.get("metadata") or {}),
        }
        state["customers"][cid] = cust
        return json_response(200, cust)

    def _new_charge(
        self, state: dict, *, amount: int, currency: str, customer: str | None,
        metadata: dict[str, Any], payment_intent: str | None = None, description: str | None = None,
    ) -> dict[str, Any]:
        ch_id = f"ch_{next_seq(state, '_ch_seq')}"
        charge = {
            "id": ch_id, "object": "charge", "amount": amount, "amount_refunded": 0,
            "currency": currency, "customer": customer, "description": description,
            "metadata": dict(metadata), "paid": True, "payment_intent": payment_intent,
            "refunded": False, "status": "succeeded", "created": self._now(),
        }
        state["charges"][ch_id] = charge
        return charge

    def _validate_charge_params(self, params: dict[str, Any], state: dict) -> tuple[int, str, str | None] | Response:
        amount = parse_int(params.get("amount"))
        if amount is None or amount <= 0:
            return _err(400, "invalid_request_error", "Invalid positive integer for parameter: amount.",
                        code="parameter_invalid_integer", param="amount")
        currency = params.get("currency")
        if not isinstance(currency, str) or len(currency.strip()) != 3:
            return _err(400, "invalid_request_error", "Missing or invalid required param: currency.",
                        code="parameter_missing", param="currency")
        customer = params.get("customer")
        if customer is not None and customer not in state["customers"]:
            return _err(400, "invalid_request_error", f"No such customer: '{customer}'",
                        code="resource_missing", param="customer")
        return amount, currency.strip().lower(), customer

    def _create_payment_intent(self, params: dict[str, Any], state: dict) -> Response:
        res = self._validate_charge_params(params, state)
        if isinstance(res, Response):
            return res
        amount, currency, customer = res
        confirm, pm = parse_bool(params.get("confirm")), params.get("payment_method")
        if confirm and pm == "pm_card_chargeDeclined":
            return _err(402, "card_error", "Your card was declined.",
                        code="card_declined", decline_code="generic_decline")
        pi_id = f"pi_{next_seq(state, '_pi_seq')}"
        metadata = dict(params.get("metadata") or {})
        latest_charge, amount_received = None, 0
        if confirm:
            status, amount_received = "succeeded", amount
            latest_charge = self._new_charge(
                state, amount=amount, currency=currency, customer=customer,
                metadata=metadata, payment_intent=pi_id, description=params.get("description"),
            )["id"]
        else:
            status = "requires_confirmation" if pm else "requires_payment_method"
        pi = {
            "id": pi_id, "object": "payment_intent", "amount": amount,
            "amount_received": amount_received, "currency": currency, "customer": customer,
            "description": params.get("description"), "latest_charge": latest_charge,
            "metadata": metadata, "payment_method": pm, "status": status, "created": self._now(),
        }
        state["payment_intents"][pi_id] = pi
        return json_response(200, pi)

    def _confirm_payment_intent(self, pi_id: str, params: dict[str, Any], state: dict) -> Response:
        pi = state["payment_intents"].get(pi_id)
        if pi is None:
            return _err(404, "invalid_request_error", f"No such payment_intent: '{pi_id}'",
                        code="resource_missing")
        if pi["status"] in ("succeeded", "canceled"):
            return _err(400, "invalid_request_error",
                        f"You cannot confirm this PaymentIntent because it has status '{pi['status']}'.",
                        code="payment_intent_unexpected_state")
        pm = params.get("payment_method") or pi.get("payment_method")
        if pm == "pm_card_chargeDeclined":
            return _err(402, "card_error", "Your card was declined.",
                        code="card_declined", decline_code="generic_decline")
        pi.update(payment_method=pm, status="succeeded", amount_received=pi["amount"])
        pi["latest_charge"] = self._new_charge(
            state, amount=pi["amount"], currency=pi["currency"], customer=pi["customer"],
            metadata=pi["metadata"], payment_intent=pi_id, description=pi.get("description"),
        )["id"]
        return json_response(200, pi)

    def _cancel_payment_intent(self, pi_id: str, state: dict) -> Response:
        pi = state["payment_intents"].get(pi_id)
        if pi is None:
            return _err(404, "invalid_request_error", f"No such payment_intent: '{pi_id}'",
                        code="resource_missing")
        if pi["status"] in ("succeeded", "canceled"):
            return _err(400, "invalid_request_error",
                        f"You cannot cancel this PaymentIntent because it has status '{pi['status']}'.",
                        code="payment_intent_unexpected_state")
        pi["status"] = "canceled"
        return json_response(200, pi)

    def _create_charge(self, params: dict[str, Any], state: dict) -> Response:
        res = self._validate_charge_params(params, state)
        if isinstance(res, Response):
            return res
        amount, currency, customer = res
        if params.get("source") == "tok_chargeDeclined":
            return _err(402, "card_error", "Your card was declined.",
                        code="card_declined", decline_code="generic_decline")
        return json_response(200, self._new_charge(
            state, amount=amount, currency=currency, customer=customer,
            metadata=dict(params.get("metadata") or {}), description=params.get("description"),
        ))

    def _create_refund(self, params: dict[str, Any], state: dict) -> Response:
        charge_id, pi_id = params.get("charge"), params.get("payment_intent")
        if not charge_id and not pi_id:
            return _err(400, "invalid_request_error",
                        "One of the following params should be provided: payment_intent or charge.",
                        code="parameter_missing")
        if charge_id:
            charge = state["charges"].get(charge_id)
            if charge is None:
                return _err(404, "invalid_request_error", f"No such charge: '{charge_id}'",
                            code="resource_missing", param="charge")
        else:
            pi = state["payment_intents"].get(pi_id)
            if pi is None:
                return _err(404, "invalid_request_error", f"No such payment_intent: '{pi_id}'",
                            code="resource_missing", param="payment_intent")
            if not pi.get("latest_charge"):
                return _err(400, "invalid_request_error",
                            "This PaymentIntent does not have a successful charge to refund.",
                            code="charge_not_found")
            charge = state["charges"][pi["latest_charge"]]

        remaining = charge["amount"] - charge["amount_refunded"]
        if remaining <= 0:
            return _err(400, "invalid_request_error",
                        f"Charge {charge['id']} has already been refunded.",
                        code="charge_already_refunded")
        req_amount = remaining if params.get("amount") is None else parse_int(params.get("amount"))
        if req_amount is None or req_amount <= 0:
            return _err(400, "invalid_request_error", "Invalid positive integer for parameter: amount.",
                        code="parameter_invalid_integer", param="amount")
        if req_amount > remaining:
            return _err(400, "invalid_request_error",
                        f"Refund amount ({req_amount}) is greater than unrefunded charge "
                        f"amount ({remaining}).", code="amount_too_large", param="amount")

        charge["amount_refunded"] += req_amount
        charge["refunded"] = charge["amount_refunded"] == charge["amount"]
        re_id = f"re_{next_seq(state, '_re_seq')}"
        refund = {
            "id": re_id, "object": "refund", "amount": req_amount, "charge": charge["id"],
            "currency": charge["currency"], "metadata": dict(params.get("metadata") or {}),
            "payment_intent": charge.get("payment_intent"), "reason": params.get("reason"),
            "status": "succeeded", "created": self._now(),
        }
        state["refunds"][re_id] = refund
        return json_response(200, refund)

    def invariants(self) -> list[Invariant]:
        """Default Stripe invariants: at-most-once charge/refund per scope and refunds <= charges."""
        initial = dict(self._initial_charges)

        def _amt(e: dict[str, Any]) -> int | None:
            for src in (e.get("result"), e.get("args")):
                if isinstance(src, Mapping) and (v := parse_int(src.get("amount"))) is not None:
                    return v
            return None

        def refunds_le_charges(world: World) -> str | None:
            charged, charged_by_id = dict(initial), {}
            for e in world.effects("stripe.charge"):
                if (amt := _amt(e)) is not None:
                    charged[e["scope"]] = charged.get(e["scope"], 0) + amt
                    if isinstance(res := e.get("result"), Mapping):
                        cid = res.get("latest_charge") or (res.get("id") if res.get("object") == "charge" else None)
                        if isinstance(cid, str):
                            charged_by_id[cid] = amt
            refunded: dict[str, int] = {}
            refunded_by_id: dict[str, int] = {}
            for e in world.effects("stripe.refund"):
                if (amt := _amt(e)) is not None:
                    refunded[e["scope"]] = refunded.get(e["scope"], 0) + amt
                    if isinstance(res := e.get("result"), Mapping) and isinstance(res.get("charge"), str):
                        refunded_by_id[res["charge"]] = refunded_by_id.get(res["charge"], 0) + amt
            for cid, total in refunded_by_id.items():
                if cid in charged_by_id and total > charged_by_id[cid]:
                    return f"stripe.refund totaled {total} for charge {cid!r}, exceeding charged amount {charged_by_id[cid]}"
            for scope, total in refunded.items():
                if scope in charged and total > charged[scope]:
                    return f"stripe.refund totaled {total} for scope {scope!r}, exceeding charged amount {charged[scope]}"
            return None

        return [
            at_most_once("stripe.charge"),
            at_most_once("stripe.refund"),
            Invariant("stripe_refunds_le_charges", refunds_le_charges,
                      ("stripe.charge", "stripe.refund"), duplicate_sensitive=True),
        ]
