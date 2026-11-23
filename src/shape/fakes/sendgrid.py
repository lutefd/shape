"""SendGrid v3 Mail Send API test double (`api.sendgrid.com`).

Mirrors SendGrid Web API v3 (`POST /v3/mail/send`):
https://www.twilio.com/docs/sendgrid/api-reference/mail-send/mail-send

Accepted sends return HTTP `202 Accepted` with an empty body and an `X-Message-Id` header.
SendGrid has no idempotency key on `/v3/mail/send`: retrying after a lost ack sends a second email.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import Any

from ..boundary.invariants import Invariant, at_most_once
from ._util import header, json_response, next_seq, parse_params, parse_url
from .base import EffectInfo, Request, Response


def _sg_error(status: int, message: str, field: str | None = None) -> Response:
    return json_response(status, {"errors": [{"message": message, "field": field, "help": None}]})


def _recipients(params: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for p in params.get("personalizations") or []:
        if not isinstance(p, dict):
            continue
        for entry in p.get("to") or []:
            if isinstance(entry, dict) and isinstance(entry.get("email"), str):
                out.append(entry["email"])
    return out


class SendGridFake:
    """Faithful test double for SendGrid v3 `POST /v3/mail/send`."""

    hosts: tuple[str, ...] = ("api.sendgrid.com",)
    api_version: str = "v3"

    def __init__(
        self,
        *,
        scope: Callable[[Request, dict[str, Any]], str] | None = None,
        valid_api_keys: Collection[str] | None = None,
        hosts: tuple[str, ...] | None = None,
    ) -> None:
        self._scope_fn = scope
        self._valid_api_keys = set(valid_api_keys) if valid_api_keys is not None else None
        if hosts is not None:
            self.hosts = tuple(hosts)

    def _default_scope(self, params: dict[str, Any]) -> str:
        custom = params.get("custom_args")
        if isinstance(custom, dict):
            if "scope" in custom:
                return str(custom["scope"])
            if "order_id" in custom:
                return f"order:{custom['order_id']}"
        recips = ",".join(sorted(_recipients(params)))
        subject = str(params.get("subject") or "")
        return f"to:{recips}|subject:{subject}"

    def _scope(self, req: Request, params: dict[str, Any]) -> str:
        if self._scope_fn is not None:
            return self._scope_fn(req, params)
        return self._default_scope(params)

    def describe(self, req: Request) -> EffectInfo | None:
        path, _ = parse_url(req.url)
        if req.method.upper() != "POST" or path != "/v3/mail/send":
            return None
        params = parse_params(req)
        return EffectInfo(
            op="sendgrid.mail.send",
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
            landed=resp.status == 202,
        )

    def handle(self, req: Request, state: dict) -> Response:
        state.setdefault("messages", [])
        path, _ = parse_url(req.url)
        if req.method.upper() != "POST" or path != "/v3/mail/send":
            return _sg_error(404, "The requested resource does not exist.")

        if self._valid_api_keys is not None:
            auth = header(req.headers, "Authorization") or ""
            key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            if key not in self._valid_api_keys:
                return _sg_error(401, "The provided authorization grant is invalid, expired, or revoked")

        params = parse_params(req)
        personalizations = params.get("personalizations")
        if not isinstance(personalizations, list) or not personalizations:
            return _sg_error(
                400,
                "The personalizations field is required and must have at least one personalization.",
                "personalizations",
            )
        for idx, p in enumerate(personalizations):
            to_list = p.get("to") if isinstance(p, dict) else None
            if not isinstance(to_list, list) or not to_list:
                return _sg_error(
                    400,
                    "Each personalization must include at least one 'to' recipient.",
                    f"personalizations.{idx}.to",
                )
            for j, recip in enumerate(to_list):
                email = recip.get("email") if isinstance(recip, dict) else None
                if not isinstance(email, str) or "@" not in email:
                    return _sg_error(
                        400,
                        "Does not contain a valid address.",
                        f"personalizations.{idx}.to.{j}.email",
                    )

        sender = params.get("from")
        if not isinstance(sender, dict) or not isinstance(sender.get("email"), str) or "@" not in sender["email"]:
            return _sg_error(400, "The from object must be provided for every email send.", "from.email")

        has_template = bool(params.get("template_id"))
        if not has_template:
            top_subject = params.get("subject")
            all_have_subject = all(
                isinstance(p.get("subject"), str) and p["subject"].strip()
                for p in personalizations
                if isinstance(p, dict)
            )
            if not (isinstance(top_subject, str) and top_subject.strip()) and not all_have_subject:
                return _sg_error(400, "The subject is required.", "subject")
            content = params.get("content")
            if not isinstance(content, list) or not content or not all(
                isinstance(c, dict) and c.get("type") and c.get("value") for c in content
            ):
                return _sg_error(400, "Unless a valid template_id is provided, the content parameter is required.", "content")

        msg_id = f"sg_msg_{next_seq(state, '_sg_seq')}"
        state["messages"].append({
            "message_id": msg_id,
            "from": dict(sender),
            "to": _recipients(params),
            "subject": params.get("subject"),
            "personalizations": personalizations,
            "content": params.get("content"),
            "template_id": params.get("template_id"),
            "custom_args": params.get("custom_args"),
        })
        return Response(status=202, headers={"X-Message-Id": msg_id}, body=b"")

    def invariants(self) -> list[Invariant]:
        """Default SendGrid invariant: at most one mail send per configured scope."""
        return [at_most_once("sendgrid.mail.send")]
