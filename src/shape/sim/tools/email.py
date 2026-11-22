"""SimEmail: a send-only outbox. Sending is irreversible.

Modeled on transactional-email APIs with idempotent sends (Resend `POST /emails` with an
`Idempotency-Key` header): the same key and parameters return the original message, the same
key with different parameters is an error, and without a key every call sends a message.
"""

from __future__ import annotations

import json
from typing import Any

from ..faults import ApiError
from ..world import World
from . import ToolSpec


class SimEmail:
    name = "email"
    api = "transactional email, Resend-style POST /emails with Idempotency-Key"
    specs = (ToolSpec("email", "send", {"order": "int", "to": "str"}, irreversible=True),)

    def __init__(self, world: World) -> None:
        self.world = world

    def execute(self, op: str, args: dict[str, Any], key: str | None) -> dict[str, Any]:
        params = json.dumps(args, sort_keys=True)
        if key is not None and (op, key) in self.world.idempotency:
            seen_params, result = self.world.idempotency[(op, key)]
            if seen_params != params:
                raise ApiError("idempotency_key_reuse")
            return dict(result)
        self.world.outbox.append(dict(args))
        result = {"message_id": f"msg_{len(self.world.outbox)}"}
        self.world.effect("email", "send", f"order:{args['order']}/email", key, False, args)
        if key is not None:
            self.world.idempotency[(op, key)] = (params, result)
        return dict(result)
