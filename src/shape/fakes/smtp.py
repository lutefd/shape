"""SMTP test double (`smtplib` / RFC 5321 & RFC 5322).

Mirrors SMTP mail submission (`MAIL FROM` / `RCPT TO` / `DATA`, matching Python's
`smtplib.SMTP.sendmail` return convention of refused recipients):
- RFC 5321 (Simple Mail Transfer Protocol): https://datatracker.ietf.org/doc/html/rfc5321
- RFC 5322 (Internet Message Format): https://datatracker.ietf.org/doc/html/rfc5322

SMTP is not idempotent: every accepted submission records and sends a new message.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable, Collection
from email import policy
from email.parser import BytesParser
from typing import Any

from ..boundary.invariants import Invariant, at_most_once
from ._util import next_seq
from .base import EffectInfo, MailRequest

_REPLY_PREFIX_RE = re.compile(r"^(?:\s*(?:re|fwd|fw)\s*:\s*)+", re.IGNORECASE)
_SEND_METHODS = frozenset({"SEND", "SENDMAIL", "SMTP.SEND", "DATA"})


def _parse_message_headers(raw: bytes) -> dict[str, Any]:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    subject = str(msg.get("Subject") or "").strip()
    message_id = (str(msg.get("Message-ID") or "").strip()) or None
    in_reply_to = (str(msg.get("In-Reply-To") or "").strip()) or None
    references = (str(msg.get("References") or "").strip()) or None
    to_header = ", ".join(str(v).strip() for v in (msg.get_all("To") or []) if str(v).strip())
    norm_subject = _REPLY_PREFIX_RE.sub("", subject).strip().lower()
    if references:
        thread = references.split()[0]
    elif in_reply_to:
        thread = in_reply_to
    else:
        thread = norm_subject or "<no-subject>"
    return {
        "subject": subject,
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "references": references,
        "to_header": to_header,
        "thread": thread,
        "norm_subject": norm_subject,
    }


def _extract_send_args(req: MailRequest) -> tuple[str, tuple[str, ...], bytes]:
    args = req.args
    from_addr = str(args[0]) if len(args) > 0 else ""
    raw_to = args[1] if len(args) > 1 else ()
    to_addrs = (raw_to,) if isinstance(raw_to, str) else tuple(str(a) for a in raw_to)
    raw_msg = args[2] if len(args) > 2 else b""
    msg_bytes = raw_msg.encode("utf-8") if isinstance(raw_msg, str) else bytes(raw_msg)
    return from_addr, to_addrs, msg_bytes


class SmtpFake:
    """Faithful SMTP test double recording Message-ID, To, Subject, and In-Reply-To."""

    hosts: tuple[str, ...] = ("smtp.example.com", "localhost")
    api_version: str = "RFC 5321 / RFC 5322"

    def __init__(
        self,
        *,
        hosts: tuple[str, ...] | None = None,
        reject_recipients: Collection[str] = (),
        scope: Callable[[MailRequest, dict[str, Any]], str] | None = None,
    ) -> None:
        if hosts is not None:
            self.hosts = tuple(hosts)
        self._reject = {addr.lower() for addr in reject_recipients}
        self._scope_fn = scope

    def send(
        self,
        from_addr: str,
        to_addrs: tuple[str, ...],
        message: bytes,
        state: dict,
    ) -> dict[str, tuple[int, bytes]]:
        """Accept an SMTP message and persist it to JSON `state`.

        Returns a dict of refused recipients (`{addr: (code, reason_bytes)}`), empty when all
        recipients were accepted, matching `smtplib.SMTP.sendmail`.
        """
        state.setdefault("messages", [])
        if not from_addr or "@" not in from_addr:
            return {addr: (553, b"5.1.7 Invalid sender address") for addr in to_addrs} or {
                "": (553, b"5.1.7 Invalid sender address")
            }
        if not to_addrs:
            return {"": (501, b"5.1.3 No recipients specified")}

        refused: dict[str, tuple[int, bytes]] = {}
        accepted: list[str] = []
        for addr in to_addrs:
            if not addr or "@" not in addr or addr.lower() in self._reject:
                refused[addr] = (550, b"5.1.1 Recipient address rejected")
            else:
                accepted.append(addr)

        if not accepted:
            return refused

        msg_bytes = message.encode("utf-8") if isinstance(message, str) else bytes(message)
        parsed = _parse_message_headers(msg_bytes)
        seq = next_seq(state, "_smtp_seq")
        host = self.hosts[0] if self.hosts else "smtp.example.com"
        message_id = parsed["message_id"] or f"<msg-{seq}@{host}>"

        state["messages"].append({
            "id": seq,
            "from": from_addr,
            "to": accepted,
            "header_to": parsed["to_header"] or ", ".join(accepted),
            "subject": parsed["subject"],
            "message_id": message_id,
            "in_reply_to": parsed["in_reply_to"],
            "references": parsed["references"],
            "thread": parsed["thread"],
            "raw_b64": base64.b64encode(msg_bytes).decode("ascii"),
        })
        return refused

    def command(
        self,
        name: str,
        args: tuple[Any, ...],
        state: dict,
    ) -> tuple[str, list[bytes | tuple | None]]:
        return ("BAD", [b"SMTP fake does not handle IMAP commands"])

    def _info(self, req: MailRequest, *, landed: bool) -> EffectInfo | None:
        if req.method.upper() not in _SEND_METHODS:
            return None
        _, to_addrs, msg_bytes = _extract_send_args(req)
        parsed = _parse_message_headers(msg_bytes)
        if self._scope_fn is not None:
            scope = self._scope_fn(req, parsed)
        elif parsed["in_reply_to"]:
            scope = f"thread:{parsed['thread']}|in_reply_to:{parsed['in_reply_to']}"
        else:
            scope = f"to:{','.join(sorted(to_addrs))}|subject:{parsed['norm_subject']}"
        op = "smtp.reply" if parsed["in_reply_to"] else "smtp.send"
        return EffectInfo(
            op=op,
            scope=scope,
            key=None,
            irreversible=True,
            landed=landed,
        )

    def describe(self, req: MailRequest) -> EffectInfo | None:
        return self._info(req, landed=False)

    def effect(self, req: MailRequest, resp: Any) -> EffectInfo | None:
        if req.method.upper() not in _SEND_METHODS:
            return None
        _, to_addrs, _ = _extract_send_args(req)
        landed = (
            bool(to_addrs)
            and isinstance(resp, dict)
            and "" not in resp
            and len(resp) < len(to_addrs)
        )
        return self._info(req, landed=landed)

    def invariants(self) -> list[Invariant]:
        """Default SMTP invariant: at most one reply per `(thread, In-Reply-To)`."""
        return [at_most_once("smtp.reply")]
