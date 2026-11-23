"""IMAP4rev1 test double (`imaplib` / RFC 3501).

Mirrors IMAP4rev1 mailbox commands (`SELECT`, `SEARCH`, `FETCH`, `STORE`, `APPEND`, `EXPUNGE`,
plus `UID` variants and session commands `LOGIN`, `LIST`, `CREATE`, `CLOSE`, `NOOP`, `LOGOUT`):
- RFC 3501 (IMAP4rev1): https://datatracker.ietf.org/doc/html/rfc3501

UIDs, messages, and flags (including `\\Seen` and `\\Deleted`) live in JSON `state` so changes
persist across crashed incarnations and concurrent actors.
"""

from __future__ import annotations

import base64
import shlex
from collections.abc import Callable, Sequence
from email import policy
from email.parser import BytesParser
from typing import Any

from ..boundary.invariants import Invariant
from .base import EffectInfo, MailRequest

_FLAG_CRITERIA: dict[str, tuple[str, bool]] = {
    "SEEN": ("\\seen", True), "UNSEEN": ("\\seen", False),
    "DELETED": ("\\deleted", True), "UNDELETED": ("\\deleted", False),
    "FLAGGED": ("\\flagged", True), "UNFLAGGED": ("\\flagged", False),
    "ANSWERED": ("\\answered", True), "UNANSWERED": ("\\answered", False),
}


def _to_str(val: Any) -> str:
    return val.decode("utf-8", errors="replace") if isinstance(val, bytes) else str(val)


def _parse_flags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return sorted({str(f) for f in raw if str(f)})
    text = _to_str(raw).strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    return sorted({tok for tok in text.split() if tok})


def _parse_set(spec: str, ids: list[int]) -> list[int]:
    if not ids:
        return []
    max_id, want = ids[-1], set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            lo_s, hi_s = part.split(":", 1)
            lo = max_id if lo_s == "*" else int(lo_s)
            hi = max_id if hi_s == "*" else int(hi_s)
            if lo > hi:
                lo, hi = hi, lo
            want.update(i for i in ids if lo <= i <= hi)
        else:
            target = max_id if part == "*" else int(part)
            if target in ids:
                want.add(target)
    return [i for i in ids if i in want]


def _matches_criteria(msg: dict[str, Any], tokens: list[str]) -> bool:
    parsed = BytesParser(policy=policy.default).parsebytes(base64.b64decode(msg["raw_b64"]))
    flags = {f.lower() for f in msg["flags"]}
    i = 0
    while i < len(tokens):
        tok = tokens[i].upper()
        if tok == "ALL":
            i += 1
        elif tok in _FLAG_CRITERIA:
            flag, expected = _FLAG_CRITERIA[tok]
            if (flag in flags) != expected:
                return False
            i += 1
        elif tok in ("FROM", "TO", "SUBJECT") and i + 1 < len(tokens):
            if tokens[i + 1].lower() not in str(parsed.get(tok) or "").lower():
                return False
            i += 2
        elif tok == "HEADER" and i + 2 < len(tokens):
            if tokens[i + 2].lower() not in str(parsed.get(tokens[i + 1]) or "").lower():
                return False
            i += 3
        else:
            i += 1
    return True


class ImapFake:
    """Faithful IMAP4rev1 test double with persistent UIDs and flags (`\\Seen`, `\\Deleted`)."""

    hosts: tuple[str, ...] = ("imap.example.com", "localhost")
    api_version: str = "RFC 3501 (IMAP4rev1)"

    def __init__(
        self, *,
        hosts: tuple[str, ...] | None = None,
        initial_messages: Sequence[bytes | dict[str, Any]] = (),
        scope: Callable[[MailRequest], str] | None = None,
    ) -> None:
        if hosts is not None:
            self.hosts = tuple(hosts)
        self._initial_messages = list(initial_messages)
        self._scope_fn = scope

    def _ensure_state(self, state: dict) -> None:
        if "mailboxes" not in state:
            msgs, uid = [], 1
            for item in self._initial_messages:
                if isinstance(item, bytes):
                    raw_b64, flags = base64.b64encode(item).decode("ascii"), []
                else:
                    raw = item.get("raw", b"")
                    raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
                    raw_b64 = item.get("raw_b64") or base64.b64encode(raw_bytes).decode("ascii")
                    flags = _parse_flags(item.get("flags"))
                msgs.append({"uid": uid, "flags": flags, "raw_b64": raw_b64})
                uid += 1
            state["mailboxes"] = {"INBOX": {"uidvalidity": 1, "next_uid": uid, "messages": msgs}}
        state.setdefault("selected", None)

    def send(
        self, from_addr: str, to_addrs: tuple[str, ...], message: bytes, state: dict,
    ) -> dict[str, tuple[int, bytes]]:
        return {"": (502, b"IMAP fake does not accept SMTP send")}

    def command(
        self, name: str, args: tuple[Any, ...], state: dict,
    ) -> tuple[str, list[bytes | tuple | None]]:
        self._ensure_state(state)
        cmd = name.upper()
        if cmd in ("LOGIN", "NOOP"):
            return ("OK", [f"{cmd} completed".encode()])
        if cmd == "LOGOUT":
            state["selected"] = None
            return ("OK", [b"LOGOUT completed"])
        if cmd == "LIST":
            return ("OK", [f'() "/" "{mb}"'.encode() for mb in sorted(state["mailboxes"])])
        if cmd == "CREATE":
            mb = _to_str(args[0]) if args else ""
            if not mb or mb in state["mailboxes"]:
                return ("NO", [b"Mailbox already exists or invalid name"])
            state["mailboxes"][mb] = {"uidvalidity": 1, "next_uid": 1, "messages": []}
            return ("OK", [b"CREATE completed"])
        if cmd in ("SELECT", "EXAMINE"):
            mb = _to_str(args[0]) if args else "INBOX"
            if mb not in state["mailboxes"]:
                return ("NO", [f"Mailbox '{mb}' does not exist".encode()])
            state["selected"] = mb
            return ("OK", [str(len(state["mailboxes"][mb]["messages"])).encode()])
        if cmd == "APPEND":
            return self._append(args, state)
        if cmd == "UID":
            if not args:
                return ("BAD", [b"UID requires a subcommand"])
            return self._mailbox_cmd(_to_str(args[0]).upper(), args[1:], state, by_uid=True)
        return self._mailbox_cmd(cmd, args, state, by_uid=False)

    def _mailbox_cmd(
        self, cmd: str, args: tuple[Any, ...], state: dict, *, by_uid: bool,
    ) -> tuple[str, list[bytes | tuple | None]]:
        sel = state.get("selected")
        mbox = state["mailboxes"].get(sel) if sel else None
        if mbox is None:
            return ("NO", [b"No mailbox selected"])
        if cmd == "SEARCH":
            return self._search(mbox, args, by_uid=by_uid)
        if cmd == "FETCH":
            return self._fetch(mbox, args, by_uid=by_uid)
        if cmd == "STORE":
            return self._store(mbox, args, by_uid=by_uid)
        if cmd == "EXPUNGE":
            return self._expunge(mbox)
        if cmd == "CLOSE":
            self._expunge(mbox)
            state["selected"] = None
            return ("OK", [b"CLOSE completed"])
        return ("BAD", [f"Unsupported IMAP command: {cmd}".encode()])

    def _search(
        self, mbox: dict[str, Any], args: tuple[Any, ...], *, by_uid: bool,
    ) -> tuple[str, list[bytes | tuple | None]]:
        raw_args = list(args)
        if raw_args and _to_str(raw_args[0]).upper() == "CHARSET":
            raw_args = raw_args[2:]
        elif raw_args and (raw_args[0] is None or _to_str(raw_args[0]).upper() in ("UTF-8", "US-ASCII", "ASCII")):
            raw_args = raw_args[1:]
        tokens = [t for a in raw_args for t in shlex.split(_to_str(a))] or ["ALL"]
        matched = [
            str(msg["uid"] if by_uid else idx)
            for idx, msg in enumerate(mbox["messages"], start=1)
            if _matches_criteria(msg, tokens)
        ]
        return ("OK", [" ".join(matched).encode()])

    def _resolve_targets(
        self, mbox: dict[str, Any], msg_set: str, *, by_uid: bool,
    ) -> list[tuple[int, dict[str, Any]]]:
        msgs = mbox["messages"]
        ids = [m["uid"] for m in msgs] if by_uid else list(range(1, len(msgs) + 1))
        chosen = set(_parse_set(msg_set, ids))
        return [(seq, m) for seq, m in enumerate(msgs, start=1) if (m["uid"] if by_uid else seq) in chosen]

    def _fetch(
        self, mbox: dict[str, Any], args: tuple[Any, ...], *, by_uid: bool,
    ) -> tuple[str, list[bytes | tuple | None]]:
        if len(args) < 2:
            return ("BAD", [b"FETCH requires message set and items"])
        msg_set, items_upper = _to_str(args[0]), _to_str(args[1]).upper()
        try:
            targets = self._resolve_targets(mbox, msg_set, by_uid=by_uid)
        except ValueError:
            return ("BAD", [f"Invalid message set: {msg_set}".encode()])
        want_body = any(k in items_upper for k in ("RFC822", "BODY[", "BODY.PEEK["))
        is_peek, header_only = "BODY.PEEK" in items_upper, "HEADER" in items_upper
        out: list[bytes | tuple | None] = []
        for seq, msg in targets:
            if want_body and not is_peek and "\\Seen" not in msg["flags"]:
                msg["flags"] = sorted({*msg["flags"], "\\Seen"})
            flags_str = " ".join(msg["flags"])
            if want_body:
                raw = base64.b64decode(msg["raw_b64"])
                if header_only:
                    sep = b"\r\n\r\n" if b"\r\n\r\n" in raw else b"\n\n"
                    payload = raw.split(sep, 1)[0] + sep
                else:
                    payload = raw
                meta = f"{seq} (UID {msg['uid']} FLAGS ({flags_str}) RFC822 {{{len(payload)}}})".encode()
                out.extend(((meta, payload), b")"))
            else:
                out.append(f"{seq} (UID {msg['uid']} FLAGS ({flags_str}))".encode())
        return ("OK", out)

    def _store(
        self, mbox: dict[str, Any], args: tuple[Any, ...], *, by_uid: bool,
    ) -> tuple[str, list[bytes | tuple | None]]:
        if len(args) < 3:
            return ("BAD", [b"STORE requires message set, command, and flags"])
        msg_set, op = _to_str(args[0]), _to_str(args[1]).upper()
        silent, base_op = op.endswith(".SILENT"), op.removesuffix(".SILENT")
        if base_op not in ("+FLAGS", "-FLAGS", "FLAGS"):
            return ("BAD", [f"Unsupported STORE operation: {op}".encode()])
        try:
            targets = self._resolve_targets(mbox, msg_set, by_uid=by_uid)
        except ValueError:
            return ("BAD", [f"Invalid message set: {msg_set}".encode()])
        new_flags = _parse_flags(args[2])
        out: list[bytes | tuple | None] = []
        for seq, msg in targets:
            cur = set(msg["flags"])
            if base_op == "+FLAGS":
                cur.update(new_flags)
            elif base_op == "-FLAGS":
                cur.difference_update(new_flags)
            else:
                cur = set(new_flags)
            msg["flags"] = sorted(cur)
            if not silent:
                out.append(f"{seq} (UID {msg['uid']} FLAGS ({' '.join(msg['flags'])}))".encode())
        return ("OK", out or [None])

    def _append(self, args: tuple[Any, ...], state: dict) -> tuple[str, list[bytes | tuple | None]]:
        if len(args) < 2:
            return ("BAD", [b"APPEND requires mailbox and message"])
        mb = _to_str(args[0])
        if mb not in state["mailboxes"]:
            return ("NO", [f"[TRYCREATE] Mailbox '{mb}' does not exist".encode()])
        no_flags = len(args) == 2 or (
            len(args) == 3
            and isinstance(args[1], str)
            and not args[1].strip().startswith(("(", "\\"))
        )
        flags = [] if no_flags else _parse_flags(args[1])
        raw = args[-1]
        raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        mbox = state["mailboxes"][mb]
        uid = int(mbox["next_uid"])
        mbox["next_uid"] = uid + 1
        mbox["messages"].append({
            "uid": uid, "flags": flags, "raw_b64": base64.b64encode(raw_bytes).decode("ascii"),
        })
        return ("OK", [f"[APPENDUID {mbox['uidvalidity']} {uid}] APPEND completed".encode()])

    def _expunge(self, mbox: dict[str, Any]) -> tuple[str, list[bytes | tuple | None]]:
        expunged: list[bytes | tuple | None] = []
        kept, offset = [], 0
        for idx, msg in enumerate(mbox["messages"], start=1):
            if "\\Deleted" in msg["flags"]:
                expunged.append(str(idx - offset).encode())
                offset += 1
            else:
                kept.append(msg)
        mbox["messages"] = kept
        return ("OK", expunged or [None])

    def _classify(self, req: MailRequest) -> tuple[str, str, str, bool] | None:
        cmd, args = req.method.upper(), req.args
        if cmd == "UID" and args:
            cmd, args = _to_str(args[0]).upper(), args[1:]
        if cmd == "STORE":
            return ("imap.store", f"msg:{_to_str(args[0]) if args else ''}", "msg", False)
        if cmd == "APPEND":
            return ("imap.append", f"mailbox:{_to_str(args[0]) if args else 'INBOX'}", "mailbox", False)
        if cmd == "EXPUNGE":
            return ("imap.expunge", "mailbox:selected", "mailbox:selected", True)
        return None

    def describe(self, req: MailRequest) -> EffectInfo | None:
        info = self._classify(req)
        if info is None:
            return None
        op, default_scope, rule, irreversible = info
        if self._scope_fn:
            return EffectInfo(op, self._scope_fn(req), None, irreversible, False, "explicit", None)
        return EffectInfo(op, default_scope, None, irreversible, False, "fake_default", rule)

    def effect(self, req: MailRequest, resp: Any) -> EffectInfo | None:
        info = self._classify(req)
        if info is None:
            return None
        op, default_scope, rule, irreversible = info
        landed = isinstance(resp, tuple) and len(resp) >= 1 and str(resp[0]).upper() == "OK"
        if self._scope_fn:
            return EffectInfo(op, self._scope_fn(req), None, irreversible, landed, "explicit", None)
        return EffectInfo(op, default_scope, None, irreversible, landed, "fake_default", rule)

    def invariants(self) -> list[Invariant]:
        return []
