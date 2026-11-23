"""Twilio Programmable Messaging REST API test double (`api.twilio.com`).

Mirrors Twilio REST API version `2010-04-01` (Message resource):
https://www.twilio.com/docs/messaging/api/message-resource

`POST /2010-04-01/Accounts/{AccountSid}/Messages.json` accepts form-encoded or JSON bodies,
returns HTTP `201 Created` with a deterministic 34-character `SM...` message SID, and is NOT
idempotent: retrying after a lost ack sends a second SMS with a new SID.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from typing import Any

from ..boundary.invariants import Invariant, at_most_once
from ._util import json_response, next_seq, parse_params, parse_url
from .base import EffectInfo, Request, Response

_MESSAGES_COL_RE = re.compile(r"^/2010-04-01/Accounts/([^/]+)/Messages\.json$")
_MESSAGE_ITEM_RE = re.compile(r"^/2010-04-01/Accounts/([^/]+)/Messages/([^/]+)\.json$")


def _twilio_err(status: int, code: int, message: str) -> Response:
    return json_response(
        status,
        {
            "code": code,
            "message": message,
            "more_info": f"https://www.twilio.com/docs/errors/{code}",
            "status": status,
        },
    )


def _valid_phone(number: Any) -> bool:
    if not isinstance(number, str):
        return False
    cleaned = number.strip().removeprefix("whatsapp:")
    digits = [ch for ch in cleaned if ch.isdigit()]
    return len(digits) >= 7


class TwilioFake:
    """Faithful test double for Twilio SMS `POST/GET /2010-04-01/Accounts/{sid}/Messages.json`."""

    hosts: tuple[str, ...] = ("api.twilio.com",)
    api_version: str = "2010-04-01"

    def __init__(
        self,
        *,
        scope: Callable[[Request, dict[str, Any]], str] | None = None,
        invalid_numbers: Collection[str] = (),
        hosts: tuple[str, ...] | None = None,
    ) -> None:
        self._scope_fn = scope
        self._invalid_numbers = set(invalid_numbers)
        if hosts is not None:
            self.hosts = tuple(hosts)

    def _scope(self, req: Request, params: dict[str, Any]) -> str:
        if self._scope_fn is not None:
            return self._scope_fn(req, params)
        to = str(params.get("To") or params.get("to") or "")
        return f"to:{to}"

    def describe(self, req: Request) -> EffectInfo | None:
        path, _ = parse_url(req.url)
        if req.method.upper() != "POST" or not _MESSAGES_COL_RE.match(path):
            return None
        params = parse_params(req)
        return EffectInfo(
            op="twilio.message.create",
            scope=self._scope(req, params),
            key=None,
            irreversible=True,
            landed=False,
        )

    def effect(self, req: Request, resp: Response) -> EffectInfo | None:
        info = self.describe(req)
        if info is None:
            return None
        return EffectInfo(
            op=info.op,
            scope=info.scope,
            key=None,
            irreversible=True,
            landed=resp.status == 201,
        )

    def handle(self, req: Request, state: dict) -> Response:
        state.setdefault("messages", {})
        method = req.method.upper()
        path, query = parse_url(req.url)

        if m := _MESSAGES_COL_RE.match(path):
            account_sid = m.group(1)
            if method == "POST":
                return self._create_message(account_sid, parse_params(req), state)
            if method == "GET":
                to_filter = (query.get("To") or [None])[-1]
                from_filter = (query.get("From") or [None])[-1]
                msgs = [
                    msg for msg in state["messages"].values()
                    if msg["account_sid"] == account_sid
                    and (to_filter is None or msg["to"] == to_filter)
                    and (from_filter is None or msg["from"] == from_filter)
                ]
                return json_response(200, {"messages": msgs, "uri": path})

        if m := _MESSAGE_ITEM_RE.match(path):
            account_sid, sid = m.group(1), m.group(2)
            if method == "GET":
                msg = state["messages"].get(sid)
                if msg is None or msg["account_sid"] != account_sid:
                    return _twilio_err(
                        404, 20404, f"The requested resource {path} was not found"
                    )
                return json_response(200, msg)

        return _twilio_err(404, 20404, f"The requested resource {path} was not found")

    def _create_message(
        self, account_sid: str, params: dict[str, Any], state: dict
    ) -> Response:
        to = params.get("To")
        if not isinstance(to, str) or not to.strip():
            return _twilio_err(400, 21604, "A 'To' phone number is required.")
        to = to.strip()
        if to in self._invalid_numbers or not _valid_phone(to):
            return _twilio_err(400, 21211, f"The 'To' number {to} is not a valid phone number.")

        from_num = params.get("From")
        msid = params.get("MessagingServiceSid")
        if not (isinstance(from_num, str) and from_num.strip()) and not (
            isinstance(msid, str) and msid.strip()
        ):
            return _twilio_err(
                400, 21603, "A 'From' phone number or 'MessagingServiceSid' is required."
            )

        body = params.get("Body")
        media = params.get("MediaUrl")
        if not (isinstance(body, str) and body) and not media:
            return _twilio_err(400, 21602, "Message body is required.")

        seq = next_seq(state, "_twilio_seq")
        sid = f"SM{seq:032x}"
        uri = f"/2010-04-01/Accounts/{account_sid}/Messages/{sid}.json"
        msg = {
            "sid": sid,
            "account_sid": account_sid,
            "to": to,
            "from": from_num.strip() if isinstance(from_num, str) and from_num.strip() else None,
            "messaging_service_sid": msid.strip() if isinstance(msid, str) and msid.strip() else None,
            "body": body if isinstance(body, str) else "",
            "status": "queued",
            "direction": "outbound-api",
            "num_segments": "1",
            "error_code": None,
            "error_message": None,
            "uri": uri,
        }
        state["messages"][sid] = msg
        return json_response(201, msg)

    def invariants(self) -> list[Invariant]:
        """Default Twilio invariant: at most one SMS created per configured scope."""
        return [at_most_once("twilio.message.create")]
