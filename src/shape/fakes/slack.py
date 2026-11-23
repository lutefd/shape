"""Slack Web API test double (`slack.com` / `api.slack.com`).

Mirrors Slack Web API methods (unversioned; current 2024 specification):
- `chat.postMessage`: https://api.slack.com/methods/chat.postMessage
- `chat.update`: https://api.slack.com/methods/chat.update
- `conversations.history`: https://api.slack.com/methods/conversations.history

Like the real Slack Web API, application errors (unknown channel, missing text, invalid auth,
message not found) return HTTP 200 with `{"ok": false, "error": "..."}`. `effect()` inspects
`ok` in the response body so rejected calls always report `landed=False`.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import Any

from ..boundary.invariants import Invariant, at_most_once
from ._util import (
    header,
    json_response,
    next_seq,
    parse_int,
    parse_params,
    parse_url,
    response_json,
)
from .base import EffectInfo, Request, Response

_BASE_TS = 1_700_000_000


def _slack_err(code: str, status: int = 200) -> Response:
    return json_response(status, {"ok": False, "error": code})


class SlackFake:
    """Faithful test double for Slack `chat.postMessage`, `chat.update`, `conversations.history`."""

    hosts: tuple[str, ...] = ("slack.com", "api.slack.com")
    api_version: str = "Slack Web API (2024)"

    def __init__(
        self,
        *,
        scope: Callable[[Request, dict[str, Any]], str] | None = None,
        channels: Collection[str] | None = None,
        valid_tokens: Collection[str] | None = None,
        hosts: tuple[str, ...] | None = None,
    ) -> None:
        self._scope_fn = scope
        self._channels = set(channels) if channels is not None else None
        self._valid_tokens = set(valid_tokens) if valid_tokens is not None else None
        if hosts is not None:
            self.hosts = tuple(hosts)

    def _method_name(self, path: str) -> str:
        if path.startswith("/api/"):
            return path[len("/api/"):]
        return path.lstrip("/")

    def _extract_params(self, req: Request) -> dict[str, Any]:
        _, query = parse_url(req.url)
        params = {k: vals[-1] for k, vals in query.items() if vals}
        params.update(parse_params(req))
        return params

    def _check_auth(self, req: Request, params: dict[str, Any]) -> Response | None:
        if self._valid_tokens is None:
            return None
        auth = header(req.headers, "Authorization") or ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else str(params.get("token") or "")
        if token not in self._valid_tokens:
            return _slack_err("invalid_auth")
        return None

    def _default_scope(self, api_method: str, params: dict[str, Any]) -> tuple[str, str]:
        channel = str(params.get("channel") or "")
        if api_method == "chat.update":
            return f"channel:{channel}:ts:{params.get('ts', '')}", "channel:ts"
        meta = params.get("metadata")
        if isinstance(meta, dict):
            payload = meta.get("event_payload")
            if isinstance(payload, dict) and "scope" in payload:
                return str(payload["scope"]), "metadata.scope"
            if "scope" in meta:
                return str(meta["scope"]), "metadata.scope"
        thread_ts = params.get("thread_ts")
        if thread_ts:
            return f"channel:{channel}:thread:{thread_ts}", "channel:thread"
        return f"channel:{channel}", "channel"

    def _scope(self, req: Request, api_method: str, params: dict[str, Any]) -> tuple[str, str, str | None]:
        if self._scope_fn is not None:
            return self._scope_fn(req, params), "explicit", None
        scope, rule = self._default_scope(api_method, params)
        return scope, "fake_default", rule

    def describe(self, req: Request) -> EffectInfo | None:
        path, _ = parse_url(req.url)
        api_method = self._method_name(path)
        params = self._extract_params(req)
        if api_method == "chat.postMessage":
            scope, scope_source, scope_rule = self._scope(req, api_method, params)
            return EffectInfo(
                op="slack.chat.postMessage",
                scope=scope,
                key=None,
                irreversible=True,
                landed=False,
                scope_source=scope_source,
                scope_rule=scope_rule,
            )
        if api_method == "chat.update":
            scope, scope_source, scope_rule = self._scope(req, api_method, params)
            return EffectInfo(
                op="slack.chat.update",
                scope=scope,
                key=None,
                irreversible=False,
                landed=False,
                scope_source=scope_source,
                scope_rule=scope_rule,
            )
        return None

    def effect(self, req: Request, resp: Response) -> EffectInfo | None:
        info = self.describe(req)
        if info is None:
            return None
        body = response_json(resp)
        landed = 200 <= resp.status < 300 and bool(body and body.get("ok") is True)
        return EffectInfo(
            op=info.op,
            scope=info.scope,
            key=info.key,
            irreversible=info.irreversible,
            landed=landed,
            scope_source=info.scope_source,
            scope_rule=info.scope_rule,
        )

    def handle(self, req: Request, state: dict) -> Response:
        state.setdefault("channels", {})
        path, _ = parse_url(req.url)
        api_method = self._method_name(path)
        params = self._extract_params(req)

        if auth_err := self._check_auth(req, params):
            return auth_err

        if api_method == "chat.postMessage":
            return self._post_message(params, state)
        if api_method == "chat.update":
            return self._update_message(params, state)
        if api_method == "conversations.history":
            return self._conversations_history(params, state)
        return _slack_err("unknown_method")

    def _valid_channel(self, channel: Any) -> str | None:
        if not isinstance(channel, str) or not channel.strip():
            return None
        ch = channel.strip()
        if self._channels is not None and ch not in self._channels:
            return None
        return ch

    def _has_content(self, params: dict[str, Any]) -> bool:
        return bool(params.get("text") or params.get("blocks") or params.get("attachments"))

    def _post_message(self, params: dict[str, Any], state: dict) -> Response:
        ch = self._valid_channel(params.get("channel"))
        if ch is None:
            return _slack_err("channel_not_found")
        if not self._has_content(params):
            return _slack_err("no_text")
        seq = next_seq(state, "_slack_seq")
        ts = f"{_BASE_TS + seq}.{seq:06d}"
        msg: dict[str, Any] = {
            "type": "message",
            "ts": ts,
            "channel": ch,
            "text": str(params.get("text") or ""),
        }
        if "blocks" in params:
            msg["blocks"] = params["blocks"]
        if "attachments" in params:
            msg["attachments"] = params["attachments"]
        if params.get("thread_ts"):
            msg["thread_ts"] = str(params["thread_ts"])
        if "metadata" in params:
            msg["metadata"] = params["metadata"]
        state["channels"].setdefault(ch, []).append(msg)
        return json_response(200, {"ok": True, "channel": ch, "ts": ts, "message": msg})

    def _update_message(self, params: dict[str, Any], state: dict) -> Response:
        ch = self._valid_channel(params.get("channel"))
        if ch is None:
            return _slack_err("channel_not_found")
        ts = str(params.get("ts") or "")
        if not ts:
            return _slack_err("message_not_found")
        if not self._has_content(params):
            return _slack_err("no_text")
        for msg in state["channels"].get(ch, []):
            if msg["ts"] == ts:
                if "text" in params:
                    msg["text"] = str(params["text"])
                if "blocks" in params:
                    msg["blocks"] = params["blocks"]
                if "attachments" in params:
                    msg["attachments"] = params["attachments"]
                return json_response(
                    200,
                    {"ok": True, "channel": ch, "ts": ts, "text": msg.get("text", ""), "message": msg},
                )
        return _slack_err("message_not_found")

    def _conversations_history(self, params: dict[str, Any], state: dict) -> Response:
        ch = self._valid_channel(params.get("channel"))
        if ch is None:
            return _slack_err("channel_not_found")
        msgs = list(state["channels"].get(ch, []))
        oldest = params.get("oldest")
        latest = params.get("latest")
        if oldest is not None:
            msgs = [m for m in msgs if float(m["ts"]) >= float(oldest)]
        if latest is not None:
            msgs = [m for m in msgs if float(m["ts"]) <= float(latest)]
        msgs.reverse()  # Slack returns conversations.history newest-first
        limit = parse_int(params.get("limit"))
        has_more = False
        if limit is not None and limit >= 0 and len(msgs) > limit:
            msgs = msgs[:limit]
            has_more = True
        return json_response(200, {"ok": True, "messages": msgs, "has_more": has_more})

    def invariants(self) -> list[Invariant]:
        """Default Slack invariant: at most one `chat.postMessage` per configured scope."""
        return [at_most_once("slack.chat.postMessage")]
