"""Discord REST v10 bot messages and nonce enforcement.

https://discord.com/developers/docs/resources/message#create-message
"""

import re
from collections.abc import Callable
from hashlib import sha256

from ._util import header, json_response, next_seq, parse_params, parse_url
from ._write import ReplayedResponse, WriteFake
from .base import Response

_ROUTE = re.compile(r"/api(?:/v10)?/channels/([^/]+)/messages(?:/([^/]+))?")


class DiscordFake(WriteFake):
    hosts = ("discord.com", "discordapp.com")
    api_version = "Discord REST v10"
    once = ("discord.message.create",)

    def __init__(self, *, hosts=None, scope=None, channels=None, bot_id="100000000000000000",
                 clock: Callable[[], float] | None = None, nonce_ttl=300):
        super().__init__(hosts=hosts, scope=scope)
        self.channels = None if channels is None else set(map(str, channels))
        self.bot_id = bot_id
        self.clock = clock
        self.nonce_ttl = nonce_ttl

    def describe(self, req):
        route = _ROUTE.fullmatch(parse_url(req.url)[0])
        if not route:
            return None
        channel, ident = route.groups()
        params = parse_params(req)
        method = req.method.upper()
        if method == "POST" and not ident:
            nonce = params.get("nonce")
            return self.write(req, "discord.message.create",
                              f"{channel}:{nonce if nonce is not None else params.get('content', '')}",
                              "channel|nonce or content", irreversible=True)
        if method == "DELETE" and ident:
            return self.write(req, "discord.message.delete", f"{channel}:{ident}",
                              "channel|message_id")

    def handle(self, req, state):
        route = _ROUTE.fullmatch(parse_url(req.url)[0])
        if not route:
            return json_response(404, {"code": 0, "message": "404: Not Found"})
        channel, ident = route.groups()
        method = req.method.upper()
        if not channel.isdigit() or (self.channels is not None and channel not in self.channels):
            return json_response(404, {"code": 10003, "message": "Unknown Channel"})
        messages = state.setdefault("channels", {}).setdefault(channel, {})
        if ident and method in ("GET", "DELETE"):
            if ident not in messages:
                return json_response(404, {"code": 10008, "message": "Unknown Message"})
            if method == "DELETE":
                del messages[ident]
                return Response(204, {}, b"")
            return json_response(200, messages[ident])
        if method != "POST" or ident:
            return json_response(404, {"code": 0, "message": "404: Not Found"})
        params = parse_params(req)
        content = params.get("content", "")
        if not content and not params.get("embeds") and not params.get("components"):
            return json_response(400, {"code": 50006, "message": "Cannot send an empty message"})
        nonce = params.get("nonce")
        if (not isinstance(content, str) or len(content) > 2000
                or (nonce is not None and (type(nonce) not in (int, str) or len(str(nonce)) > 25))):
            return json_response(400, {"code": 50035, "message": "Invalid Form Body"})
        author = sha256((header(req.headers, "Authorization") or self.bot_id).encode()).hexdigest()
        nonces = state.setdefault("nonces", {}).setdefault(author, {})
        now = self.clock() if self.clock is not None else 0
        previous = nonces.get(str(nonce))
        if params.get("enforce_nonce") is True and previous and now < previous["expires"]:
            existing = state["channels"][previous["channel"]].get(previous["id"])
            if existing:
                response = json_response(200, existing)
                return ReplayedResponse(response.status, response.headers, response.body)
        ident = str(100000000000000000 + next_seq(state))
        message = {
            "id": ident, "channel_id": channel, "content": content, "type": 0, "flags": 0,
            "author": {"id": self.bot_id, "username": "shape-test", "discriminator": "0000",
                       "avatar": None, "bot": True},
            "timestamp": "2024-01-01T00:00:00+00:00", "edited_timestamp": None,
            "tts": bool(params.get("tts", False)), "mention_everyone": False, "mentions": [],
            "mention_roles": [], "attachments": [], "embeds": params.get("embeds", []),
            "pinned": False,
        }
        if nonce is not None:
            message["nonce"] = nonce
            nonces[str(nonce)] = {"id": ident, "channel": channel, "expires": now + self.nonce_ttl}
        messages[ident] = message
        return json_response(200, message)
