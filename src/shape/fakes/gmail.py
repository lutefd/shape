"""Gmail v1 messages, threads, labels and history.

https://developers.google.com/workspace/gmail/api/reference/rest
https://developers.google.com/workspace/gmail/api/guides/threads
"""

from __future__ import annotations

import base64
import binascii
import re
from copy import deepcopy
from email import policy
from email.parser import BytesParser
from urllib.parse import unquote

from ._google import google_error
from ._util import json_response, next_seq, parse_bool, parse_int, parse_params, parse_url
from ._write import WriteFake
from .base import Response

_ROUTE = re.compile(r"/gmail/v1/users/([^/]+)/(messages|threads|labels|history)(?:/(.*))?")
_SYSTEM_LABELS = (
    "INBOX", "SPAM", "TRASH", "UNREAD", "STARRED", "IMPORTANT", "SENT", "DRAFT",
    "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
)


def _parse_raw(raw):
    if not isinstance(raw, str) or not raw:
        return None
    try:
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        return BytesParser(policy=policy.default).parsebytes(decoded), len(decoded)
    except (ValueError, binascii.Error):
        return None


def _subject(text):
    return re.sub(r"^(?:re:\s*)+", "", str(text), flags=re.IGNORECASE)


def _missing():
    return google_error(404, "notFound", "Requested entity was not found.")


def _invalid(message):
    return google_error(400, "invalidArgument", message)


def _body_payload(mail):
    payload = {
        "mimeType": mail.get_content_type(), "filename": mail.get_filename() or "",
        "headers": [{"name": key, "value": str(value)} for key, value in mail.items()],
    }
    if mail.is_multipart():
        payload.update(body={"size": 0}, parts=[_body_payload(part) for part in mail.iter_parts()])
    else:
        decoded = mail.get_payload(decode=True) or b""
        payload["body"] = {"size": len(decoded), "data": base64.urlsafe_b64encode(decoded).decode()}
    return payload


class GmailFake(WriteFake):
    hosts = ("gmail.googleapis.com",)
    api_version = "Gmail API v1"
    once = ("gmail.messages.send", "gmail.labels.create")

    def __init__(self, *, hosts=None, scope=None, email="user@example.com", initial_messages=()):
        super().__init__(hosts=hosts, scope=scope)
        self.email = email.lower()
        self.initial_messages = deepcopy(tuple(initial_messages))

    def describe(self, req):
        route = _ROUTE.fullmatch(parse_url(req.url)[0])
        if not route:
            return None
        user, resource, tail = route.groups()
        user = self._user(user)
        params = parse_params(req)
        method = req.method.upper()
        if resource == "messages" and tail == "send" and method == "POST":
            parsed = _parse_raw(params.get("raw"))
            mail = parsed[0] if parsed else {}
            headers = {key: str(value) for key, value in mail.items()}
            recipient = str(mail.get("To", "")).lower()
            subject = str(mail.get("Subject", ""))
            return self.write(req, "gmail.messages.send",
                              f"{user}:{recipient}|{subject}", "mailbox|to|subject",
                              irreversible=True, params={**params, "headers": headers})
        if (resource in ("messages", "threads") and tail and tail.endswith("/modify")
                and method == "POST"):
            return self.write(req, f"gmail.{resource}.modify", f"{user}:{tail[:-7]}",
                              "mailbox|resource_id")
        if resource == "labels":
            action = {"POST": "create", "PATCH": "update", "PUT": "update",
                      "DELETE": "delete"}.get(method)
            if action and (tail is None) == (action == "create"):
                return self.write(req, f"gmail.labels.{action}",
                                  f"{user}:{tail or params.get('name', '')}", "mailbox|label")

    def _user(self, user):
        user = unquote(user).lower()
        return self.email if user == "me" else user

    def _mailbox(self, state, user):
        boxes = state.setdefault("mailboxes", {})
        if user not in boxes:
            box = {"messages": {}, "labels": {}, "history": []}
            for name in _SYSTEM_LABELS:
                box["labels"][name] = {"id": name, "name": name, "type": "system"}
            if user == self.email:
                for raw in self.initial_messages:
                    encoded = base64.urlsafe_b64encode(raw).decode()
                    self._store(box, encoded, _parse_raw(encoded), None, ["INBOX", "UNREAD"])
            boxes[user] = box
        return boxes[user]

    def _history(self, box, message, changes):
        history_id = str(next_seq(box, "history_id"))
        message["historyId"] = history_id
        ref = {"id": message["id"], "threadId": message["threadId"]}
        record = {"id": history_id, "messages": [ref]}
        for kind, labels in changes.items():
            record[kind] = [{"message": ref, **({"labelIds": labels} if labels else {})}]
        box["history"].append(record)

    def _store(self, box, raw, parsed, thread_id, labels):
        mail, size = parsed
        message_id = f"{next_seq(box, 'message_id'):016x}"
        body = mail.get_body(preferencelist=("plain",))
        snippet = ""
        if body:
            data = body.get_payload(decode=True) or b""
            try:
                text = data.decode(body.get_content_charset() or "utf-8", errors="replace")
            except LookupError:
                text = data.decode("utf-8", errors="replace")
            snippet = " ".join(text.split())[:160]
        message = {
            "id": message_id, "threadId": thread_id or message_id, "labelIds": labels,
            "snippet": snippet, "internalDate": str(1700000000000 + box["message_id"]),
            "sizeEstimate": size, "raw": raw, "payload": _body_payload(mail),
        }
        box["messages"][message_id] = message
        self._history(box, message, {"messagesAdded": []})
        return message

    @staticmethod
    def _message(message, fmt, query):
        result = dict(message)
        if fmt != "raw":
            result.pop("raw")
        if fmt in ("minimal", "raw", "metadata"):
            result.pop("payload")
        if fmt == "minimal":
            return {key: result[key] for key in ("id", "threadId", "labelIds", "historyId")}
        if fmt == "metadata":
            names = {name.lower() for name in query.get("metadataHeaders", [])}
            result["payload"] = {"headers": [
                item for item in message["payload"]["headers"]
                if not names or item["name"].lower() in names
            ]}
        return result

    @staticmethod
    def _matches(message, query):
        labels = set(message["labelIds"])
        if (not parse_bool(query.get("includeSpamTrash", ["false"])[0])
                and labels & {"SPAM", "TRASH"}):
            return False
        if not set(query.get("labelIds", ())) <= labels:
            return False
        headers = {item["name"].lower(): item["value"].lower()
                   for item in message["payload"]["headers"]}
        for term in query.get("q", [""])[0].lower().split():
            key, sep, value = term.partition(":")
            if sep and key in ("from", "to", "subject"):
                if value not in headers.get(key, ""):
                    return False
            elif sep and key in ("is", "in", "label"):
                if value.upper() not in labels:
                    return False
            elif term not in (headers.get("subject", "") + " " + message["snippet"].lower()):
                return False
        return True

    @staticmethod
    def _page(values, key, query):
        limit = parse_int(query.get("maxResults", ["100"])[0])
        offset = parse_int(query.get("pageToken", ["0"])[0])
        if limit is None or not 1 <= limit <= 500 or offset is None or offset < 0:
            return _invalid("Invalid pagination parameters")
        page = values[offset:offset + limit]
        result = {key: page} if page else {}
        if key != "history":
            result["resultSizeEstimate"] = len(values)
        if offset + limit < len(values):
            result["nextPageToken"] = str(offset + limit)
        return result

    def _modify(self, box, messages, params):
        add = params.get("addLabelIds", [])
        remove = params.get("removeLabelIds", [])
        for ids in (add, remove):
            if not isinstance(ids, list) or len(ids) > 100:
                return _invalid("Invalid label list")
            if any(not isinstance(label, str) or label not in box["labels"] for label in ids):
                return _invalid("Invalid label")
        for message in messages:
            prior = set(message["labelIds"])
            labels = (prior | set(add)) - set(remove)
            if prior != labels:
                message["labelIds"] = sorted(labels)
                changes = {key: sorted(delta) for key, delta in
                           (("labelsAdded", labels - prior), ("labelsRemoved", prior - labels))
                           if delta}
                self._history(box, message, changes)
        return None

    def _send(self, box, params):
        raw = params.get("raw")
        parsed = _parse_raw(raw)
        if parsed is None or not any(parsed[0].get(name) for name in ("To", "Cc", "Bcc")):
            return _invalid("Invalid RFC 2822 message")
        thread_id = params.get("threadId")
        if thread_id:
            previous = next((m for m in box["messages"].values()
                             if m["threadId"] == thread_id), None)
            if previous is None:
                return _missing()
            subject = next((h["value"] for h in previous["payload"]["headers"]
                            if h["name"].lower() == "subject"), "")
            mail = parsed[0]
            if (not mail.get("In-Reply-To") or not mail.get("References")
                    or _subject(mail.get("Subject", "")) != _subject(subject)):
                return _invalid("Invalid thread reply")
        message = self._store(box, raw, parsed, thread_id, ["SENT"])
        return json_response(200, {key: message[key] for key in ("id", "threadId", "labelIds")})

    def _labels(self, box, method, tail, params):
        labels = box["labels"]
        if method == "GET":
            if tail is None:
                return json_response(200, {"labels": list(labels.values())})
            label = labels.get(tail)
            if label is None:
                return _missing()
            messages = [m for m in box["messages"].values() if tail in m["labelIds"]]
            unread = [m for m in messages if "UNREAD" in m["labelIds"]]
            return json_response(200, {**label, "messagesTotal": len(messages),
                                       "messagesUnread": len(unread),
                                       "threadsTotal": len({m["threadId"] for m in messages}),
                                       "threadsUnread": len({m["threadId"] for m in unread})})
        if method == "POST" and tail is None:
            label = {"type": "user"}
        elif tail in labels and method in ("PATCH", "PUT", "DELETE"):
            label = dict(labels[tail])
            if label["type"] == "system":
                return _invalid("Cannot modify a system label")
            if method == "DELETE":
                for message in box["messages"].values():
                    if tail in message["labelIds"]:
                        self._modify(box, [message], {"removeLabelIds": [tail]})
                del labels[tail]
                return Response(204, {}, b"")
        else:
            return _missing()
        name = params.get("name", label.get("name"))
        if not isinstance(name, str) or not name.strip() or name.upper() in _SYSTEM_LABELS:
            return _invalid("Invalid label name")
        if any(existing["name"] == name and ident != tail for ident, existing in labels.items()):
            return google_error(409, "aborted", "Label name exists or conflicts")
        if tail is None:
            tail = f"Label_{next_seq(box, 'label_id')}"
        label.update({key: params[key] for key in ("labelListVisibility", "messageListVisibility",
                                                  "color") if key in params})
        label.update(id=tail, name=name)
        labels[tail] = label
        return json_response(200, label)

    def handle(self, req, state):
        path, query = parse_url(req.url)
        route = _ROUTE.fullmatch(path)
        if not route:
            return _missing()
        user, resource, tail = route.groups()
        user = self._user(user)
        box = self._mailbox(state, user)
        params = parse_params(req)
        method = req.method.upper()
        if resource == "labels":
            return self._labels(box, method, tail, params)
        if resource == "history" and method == "GET" and tail is None:
            start = parse_int(query.get("startHistoryId", [""])[0])
            if start is None or start < 1 or start > box.get("history_id", 0):
                return _missing()
            history = [record for record in box["history"] if int(record["id"]) > start]
            page = self._page(history, "history", query)
            return page if isinstance(page, Response) else json_response(
                200, {**page, "historyId": str(box["history_id"])}
            )
        if resource == "messages" and tail == "send" and method == "POST":
            return self._send(box, params)
        if resource not in ("messages", "threads"):
            return _missing()
        messages = box["messages"]
        if tail is None and method == "GET":
            matching = [m for m in reversed(list(messages.values())) if self._matches(m, query)]
            if resource == "messages":
                values = [{key: m[key] for key in ("id", "threadId")} for m in matching]
            else:
                values = list({
                    m["threadId"]: {"id": m["threadId"], "snippet": m["snippet"],
                                    "historyId": m["historyId"]}
                    for m in reversed(matching)
                }.values())[::-1]
            page = self._page(values, resource, query)
            return page if isinstance(page, Response) else json_response(200, page)
        if tail is None:
            return _missing()
        ident, _, action = tail.partition("/")
        selected = ([messages[ident]] if ident in messages else []) if resource == "messages" else [
            m for m in messages.values() if m["threadId"] == ident
        ]
        if not selected:
            return _missing()
        fmt = query.get("format", ["full"])[0]
        if fmt not in ("full", "metadata", "minimal", "raw"):
            return _invalid("Invalid message format")
        if method == "POST" and action == "modify":
            if error := self._modify(box, selected, params):
                return error
        elif method != "GET" or action:
            return _missing()
        items = [self._message(m, fmt, query) for m in selected]
        if resource == "messages":
            return json_response(200, items[0])
        return json_response(200, {"id": ident, "snippet": selected[-1]["snippet"],
                                   "historyId": str(max(int(m["historyId"]) for m in selected)),
                                   "messages": items})
