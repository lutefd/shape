"""Telegram Bot API messaging and update acknowledgment.

https://core.telegram.org/bots/api#available-methods
"""

import re
from copy import deepcopy

from ._util import json_response, next_seq, parse_int, parse_params, parse_url
from ._write import WriteFake

_ROUTE = re.compile(r"/bot([^/]+)/([^/]+)")
_WRITES = {name.lower(): name for name in ("sendMessage", "editMessageText", "sendChatAction")}
_ACTIONS = frozenset({
    "typing", "upload_photo", "record_video", "upload_video", "record_voice",
    "upload_voice", "upload_document", "choose_sticker", "find_location",
    "record_video_note", "upload_video_note",
})


def _ok(value):
    return json_response(200, {"ok": True, "result": value})


def _error(status, description):
    reason = {400: "Bad Request", 401: "Unauthorized", 404: "Not Found"}[status]
    return json_response(status, {"ok": False, "error_code": status,
                                  "description": f"{reason}: {description}" if description else reason})


class TelegramFake(WriteFake):
    hosts = ("api.telegram.org",)
    api_version = "Telegram Bot API (HTTP)"
    once = ("telegram.sendMessage",)

    def __init__(self, *, hosts=None, scope=None, initial_updates=(), chats=None, valid_tokens=None,
                 bot_id=100000, username="shape_test_bot"):
        super().__init__(hosts=hosts, scope=scope)
        self.initial_updates = sorted(deepcopy(tuple(initial_updates)), key=lambda u: u["update_id"])
        self.chats = None if chats is None else set(map(str, chats))
        self.valid_tokens = None if valid_tokens is None else set(valid_tokens)
        self.bot = {"id": bot_id, "is_bot": True, "first_name": "Shape", "username": username}

    @staticmethod
    def _write_method(path):
        route = _ROUTE.fullmatch(path)
        if not route:
            return None
        return _WRITES.get(route[2].lower())

    def is_get_write(self, req):
        return self._write_method(parse_url(req.url)[0]) is not None

    def describe(self, req):
        path, query = parse_url(req.url)
        normalized = self._write_method(path)
        if normalized is None or req.method.upper() not in ("GET", "POST"):
            return None
        params = {**{key: values[-1] for key, values in query.items()}, **parse_params(req)}
        chat = params.get("chat_id", "")
        if normalized == "sendMessage":
            return self.write(req, "telegram.sendMessage", f"{chat}:{params.get('text', '')}",
                              "chat_id|text", irreversible=True, params=params)
        return self.write(req, f"telegram.{normalized}",
                          f"{chat}:{params.get('message_id', params.get('action', ''))}",
                          "chat_id|message_id or action", params=params)

    @staticmethod
    def _updates(bot_state, params):
        offset = parse_int(params.get("offset", 0))
        limit = parse_int(params.get("limit", 100))
        if offset is None or limit is None or not 1 <= limit <= 100:
            return _error(400, "invalid offset or limit")
        queue = bot_state["updates"]
        if offset < 0:
            queue[:] = queue[offset:]
        elif offset > 0:
            queue[:] = [u for u in queue if u["update_id"] >= offset]
        return _ok(queue[:limit])

    def handle(self, req, state):
        path, query = parse_url(req.url)
        route = _ROUTE.fullmatch(path)
        if route is None or req.method.upper() not in ("GET", "POST"):
            return _error(404, "")
        token, method = route.groups()
        if self.valid_tokens is not None and token not in self.valid_tokens:
            return _error(401, "")
        method = method.lower()
        params = {**{key: values[-1] for key, values in query.items()}, **parse_params(req)}
        bot_id = parse_int(token.partition(":")[0]) or self.bot["id"]
        bot = {**self.bot, "id": bot_id}
        if method == "getme":
            return _ok(bot)
        bot_state = state.setdefault("bots", {}).setdefault(
            str(bot_id), {"updates": deepcopy(self.initial_updates), "chats": {}}
        )
        if method == "getupdates":
            return self._updates(bot_state, params)
        if method not in ("sendmessage", "editmessagetext", "sendchataction"):
            return _error(404, "")
        chat_id = params.get("chat_id")
        numeric_chat = parse_int(chat_id)
        if numeric_chat is None or (self.chats is not None and str(numeric_chat) not in self.chats):
            if not chat_id:
                return _error(400, "chat_id is empty")
            return _error(400, "chat not found")
        chat = {"id": numeric_chat, "type": "private" if numeric_chat > 0 else "supergroup"}
        if method == "sendchataction":
            if params.get("action") not in _ACTIONS:
                return _error(400, "wrong parameter action in request")
            return _ok(True)
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            return _error(400, "message text is empty")
        if len(text) > 4096:
            return _error(400, "message is too long")
        store = bot_state["chats"].setdefault(str(numeric_chat), {"messages": {}})
        if method == "sendmessage":
            message_id = next_seq(store)
            message = {"message_id": message_id, "date": 1700000000 + message_id,
                       "chat": chat, "from": bot, "text": text}
            store["messages"][str(message_id)] = message
            return _ok(message)
        message_id = parse_int(params.get("message_id"))
        message = store["messages"].get(str(message_id))
        if message is None:
            return _error(400, "message to edit not found")
        if message["text"] == text:
            return _error(400, "message is not modified")
        message.update(text=text, edit_date=1700000000 + next_seq(store, "edit_seq"))
        return _ok(message)
