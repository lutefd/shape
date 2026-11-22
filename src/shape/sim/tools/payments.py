"""SimPayments: charge and refund with Stripe idempotency semantics.

Mirrors Stripe's REST API as documented in 2024 (`POST /v1/charges`, `POST /v1/refunds`,
`Idempotency-Key`): the same key with the same parameters returns the original result
without a second effect; the same key with different parameters is an error; without a key
every call is a new charge. A charge can be refunded once. Keys never expire here.
"""

from __future__ import annotations

import json
from typing import Any

from ..faults import ApiError
from ..world import Charge, World
from . import ToolSpec


class SimPayments:
    name = "payments"
    api = "Stripe REST 2024: charges, refunds, Idempotency-Key"
    specs = (
        ToolSpec("payments", "charge", {"order": "int", "amount": "int"}),
        ToolSpec("payments", "refund", {"charge_id": "str"}),
    )

    def __init__(self, world: World) -> None:
        self.world = world

    def execute(self, op: str, args: dict[str, Any], key: str | None) -> dict[str, Any]:
        params = json.dumps(args, sort_keys=True)
        if key is not None and (op, key) in self.world.idempotency:
            seen_params, result = self.world.idempotency[(op, key)]
            if seen_params != params:
                raise ApiError("idempotency_key_reuse")
            return dict(result)
        result = self._charge(args, key) if op == "charge" else self._refund(args, key)
        if key is not None:
            self.world.idempotency[(op, key)] = (params, result)
        return dict(result)

    def _charge(self, args: dict[str, Any], key: str | None) -> dict[str, Any]:
        charge_id = f"ch_{len(self.world.charges) + 1}"
        self.world.charges[charge_id] = Charge(args["order"], args["amount"], key)
        self.world.effect("payments", "charge", f"order:{args['order']}/charge", key, True, args)
        return {"charge_id": charge_id, "amount": args["amount"]}

    def _refund(self, args: dict[str, Any], key: str | None) -> dict[str, Any]:
        charge = self.world.charges.get(args["charge_id"])
        if charge is None:
            raise ApiError("no_such_charge")
        if charge.refunded:
            raise ApiError("charge_already_refunded")
        charge.refunded = True
        self.world.effect("payments", "refund", f"order:{charge.order}/refund", key, True, args)
        return {"refunded": args["charge_id"]}
