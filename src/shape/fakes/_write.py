"""Common effect bookkeeping for HTTP provider writes."""

from dataclasses import dataclass, replace

from ..boundary.invariants import at_most_once
from ._util import parse_params
from .base import EffectInfo, Response


@dataclass(frozen=True)
class ReplayedResponse(Response):
    """A successful response that did not commit a second provider write."""


class WriteFake:
    once: tuple[str, ...] = ()
    use_idempotency_header = False

    def __init__(self, *, hosts=None, scope=None):
        self.hosts = tuple(hosts) if hosts is not None else self.hosts
        self.scope = scope

    def write(self, req, op, default_scope, rule, *, key=None, irreversible=False, params=None):
        if self.scope is not None:
            scope = self.scope(req, parse_params(req) if params is None else params)
            return EffectInfo(op, scope, key, irreversible, False, "explicit")
        return EffectInfo(op, default_scope, key, irreversible, False, "fake_default", rule)

    def effect(self, req, resp):
        info = self.describe(req)
        if info is None:
            return None
        return replace(info, landed=200 <= resp.status < 300
                       and not isinstance(resp, ReplayedResponse))

    def invariants(self):
        return [at_most_once(op) for op in self.once]
