"""Shared stdlib helpers for provider fakes."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit

from .base import Request, Response

_BRACKET_RE = re.compile(r"([^\[\]]+)|\[([^\[\]]*)\]")


def header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive HTTP header lookup."""
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def parse_url(url: str) -> tuple[str, dict[str, list[str]]]:
    """Split a URL into its normalized path (without trailing slash except '/') and query params."""
    parts = urlsplit(url)
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path, parse_qs(parts.query, keep_blank_values=True)


def _set_nested(root: dict[str, Any], parts: list[str], value: str) -> None:
    cur: Any = root
    for i, key in enumerate(parts[:-1]):
        nxt = parts[i + 1]
        is_list_idx = nxt == "" or nxt.isdigit()
        if isinstance(cur, dict):
            if key not in cur:
                cur[key] = [] if is_list_idx else {}
            cur = cur[key]
        elif isinstance(cur, list):
            idx = len(cur) if key == "" else int(key)
            while len(cur) <= idx:
                cur.append([] if is_list_idx else {})
            cur = cur[idx]
    last = parts[-1]
    if isinstance(cur, dict):
        cur[last] = value
    elif isinstance(cur, list):
        if last == "":
            cur.append(value)
        else:
            idx = int(last)
            while len(cur) <= idx:
                cur.append(None)
            cur[idx] = value


def parse_form(body: bytes) -> dict[str, Any]:
    """Parse `application/x-www-form-urlencoded`, expanding bracket notation like `metadata[k]`."""
    out: dict[str, Any] = {}
    text = body.decode("utf-8", errors="replace")
    for raw_key, value in parse_qsl(text, keep_blank_values=True):
        if "[" in raw_key and raw_key.endswith("]"):
            parts = [m.group(1) if m.group(1) is not None else m.group(2)
                     for m in _BRACKET_RE.finditer(raw_key)]
            if parts:
                _set_nested(out, parts, value)
                continue
        out[raw_key] = value
    return out


def parse_params(req: Request) -> dict[str, Any]:
    """Extract request parameters from `req.json`, JSON body, or form-encoded body."""
    if isinstance(req.json, dict):
        return dict(req.json)
    if not req.body:
        return {}
    ctype = (header(req.headers, "content-type") or "").lower()
    stripped = req.body.lstrip()
    if "json" in ctype or stripped.startswith(b"{"):
        try:
            parsed = json.loads(req.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return parse_form(req.body)


def json_response(
    status: int,
    payload: Any,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """Build a UTF-8 JSON `Response`."""
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    return Response(status=status, headers=hdrs, body=body)


def response_json(resp: Response) -> dict[str, Any] | None:
    """Decode a JSON object response body, or return None."""
    if not resp.body:
        return None
    try:
        parsed = json.loads(resp.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def next_seq(state: dict, key: str = "_seq") -> int:
    """Increment and return a deterministic integer sequence counter stored in JSON `state`."""
    val = int(state.get(key, 0)) + 1
    state[key] = val
    return val


def parse_bool(value: Any) -> bool:
    """Interpret form or JSON booleans (`True`, `'true'`, `'1'`)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def parse_int(value: Any) -> int | None:
    """Parse an integer from JSON int or form-encoded numeric string; reject booleans."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None
