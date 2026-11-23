"""Protocols and data shapes shared by provider test doubles and `shape.net`."""

from __future__ import annotations

import copy
import functools
import inspect
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Request:
    """An intercepted HTTP request passed to an `HttpFake`."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes
    json: Any | None


@dataclass(frozen=True)
class Response:
    """An HTTP response returned by an `HttpFake`."""

    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class EffectInfo:
    """Metadata describing a write effect for the shape world log and fault keys."""

    op: str
    scope: str
    key: str | None
    irreversible: bool
    landed: bool
    scope_source: str = "explicit"
    scope_rule: str | None = None


@dataclass(frozen=True)
class MailRequest:
    """An intercepted SMTP or IMAP request passed to a `MailFake`."""

    method: str
    host: str
    args: tuple[Any, ...]


@runtime_checkable
class HttpFake(Protocol):
    """Protocol implemented by HTTP provider fakes (Stripe, Slack, GitHub, SendGrid, Twilio).

    Implementations may also define an optional `describe(self, req: Request) -> EffectInfo | None`
    so `shape.net` can derive domain-specific fault keys before executing `handle`.
    """

    hosts: tuple[str, ...]
    api_version: str

    def handle(self, req: Request, state: dict) -> Response: ...
    def effect(self, req: Request, resp: Response) -> EffectInfo | None: ...
    def invariants(self) -> list: ...


@runtime_checkable
class MailFake(Protocol):
    """Protocol implemented by SMTP/IMAP provider fakes.

    Implementations may also define an optional
    `describe(self, req: MailRequest) -> EffectInfo | None` for pre-call fault keys.
    """

    hosts: tuple[str, ...]
    api_version: str

    def send(
        self,
        from_addr: str,
        to_addrs: tuple[str, ...],
        message: bytes,
        state: dict,
    ) -> dict[str, tuple[int, bytes]]: ...

    def command(
        self,
        name: str,
        args: tuple[Any, ...],
        state: dict,
    ) -> tuple[str, list[bytes | tuple | None]]: ...

    def effect(self, req: MailRequest, resp: Any) -> EffectInfo | None: ...
    def invariants(self) -> list: ...


def seedable(cls: type) -> type:
    """Let `cls(..., initial_state={...})` start the fake with that provider state.

    The dict is merged over the provider's empty state before the first call of every seed (so
    a restarted incarnation sees what the agent changed, not the seed again). Its keys are the
    provider's own state keys, the same JSON the fake keeps between calls; fakes with friendlier
    seeding (`LangGraphFake(threads=[...])`) document their own arguments.
    """
    init = cls.__init__
    cls.initial_state = None
    if "initial_state" in inspect.signature(init).parameters:
        return cls

    @functools.wraps(init)
    def __init__(self, *args: Any, initial_state: Mapping[str, Any] | None = None,
                 **kwargs: Any) -> None:
        init(self, *args, **kwargs)
        if initial_state:
            json.dumps(initial_state)  # a seed must be JSON, like the state it merges into
            self.initial_state = {**(self.initial_state or {}), **copy.deepcopy(dict(initial_state))}

    signature = inspect.signature(init)
    params = [p for p in signature.parameters.values() if p.kind is not p.VAR_KEYWORD]
    params.append(inspect.Parameter("initial_state", inspect.Parameter.KEYWORD_ONLY, default=None))
    params += [p for p in signature.parameters.values() if p.kind is p.VAR_KEYWORD]
    __init__.__signature__ = signature.replace(parameters=params)
    cls.__init__ = __init__
    return cls
