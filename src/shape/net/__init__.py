"""Capture network calls in a scenario with `@shape.scenario(net=[fake, ...])`."""

from __future__ import annotations

import contextlib
import functools
import json
import socket
import sqlite3
import ssl
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from ..boundary import api
from ..fakes.base import EffectInfo

__all__ = ["UnregisteredHost"]


class UnregisteredHost(api.HarnessError):
    """The scenario used a host without a matching fake for the protocol."""


def _host(host: str) -> str:
    return host.rstrip(".").lower()


def _providers(fakes):
    providers = {}
    for index, fake in enumerate(fakes):
        for host in dict.fromkeys(map(_host, fake.hosts)):
            if not host:
                raise ValueError("shape.net: empty fake host")
            providers.setdefault(host, []).append((index, fake))
    return providers


class _Headers(Mapping):
    def __init__(self, headers):
        self._entries = {key.lower(): (key, value) for key, value in headers.items()}

    def __getitem__(self, key):
        return self._entries[key.lower()][1]

    def __iter__(self):
        return (key for key, _ in self._entries.values())

    def __len__(self):
        return len(self._entries)


class _Capture:
    def __init__(self, workdir: Path, fakes):
        self.db = workdir / "network.sqlite3"
        self.providers = _providers(fakes)

    def reject(self, op, text, error=api.HarnessError):
        ctx = api._active
        ctx.append(ctx.world, {"kind": "harness_error", "op": op, "text": text})
        raise error("net", op, text)

    def provider(self, host, method):
        matches = [provider for provider in self.providers.get(_host(host), ())
                   if callable(getattr(provider[1], method, None))]
        if not matches:
            self.reject("net", f"shape.net: no {method} fake registered for host {host!r}",
                        UnregisteredHost)
        if len(matches) > 1:
            self.reject("net", f"shape.net: multiple {method} fakes registered for host {host!r}")
        return matches[0]

    def perform(self, req, op, args, provider, invoke, errors, *, safe, key=None, result=None):
        index, fake = provider
        # A URL or a mailbox name is more useful than a Python wrapper name in a schedule.
        call = f"{op} ({', '.join(f'{k}={api._short(v)}' for k, v in args.items())})"
        ctx = api._active
        if safe:
            api.point(call if args else op)
        elif ctx.gate:
            ctx.gate.point(call)
        describe = getattr(fake, "describe", None)
        try:
            info = describe(req) if describe is not None and not safe else None
            if info is not None and not isinstance(info, EffectInfo):
                raise TypeError("describe() must return EffectInfo or None")
        except Exception as exc:  # noqa: BLE001 - a broken provider must fail the harness
            api._harness_fail(ctx, "net.describe", describe, op, exc)

        proposed = info or EffectInfo(op, "", None, False, False)
        if not safe:
            spec = api.EffectSpec(proposed.op, None, None, proposed.irreversible, errors)
            pending = ctx.begin(spec, args, args, call, proposed.scope,
                                proposed.key if key is None else key, resolve_after=True)
        try:
            with contextlib.closing(sqlite3.connect(self.db, isolation_level=None)) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS providers (id INTEGER PRIMARY KEY, "
                           "state TEXT NOT NULL)")
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT state FROM providers WHERE id = ?", (index,)).fetchone()
                state = json.loads(row[0]) if row else {}
                response = invoke(fake, state)
                final = fake.effect(req, response) if not safe else None
                if final is not None and not isinstance(final, EffectInfo):
                    raise TypeError("effect() must return EffectInfo or None")
                data = json.dumps(state)
                if data != (row[0] if row else "{}"):
                    db.execute("INSERT OR REPLACE INTO providers VALUES (?, ?)", (index, data))
        except Exception as exc:  # noqa: BLE001 - a broken provider must fail the harness
            api._harness_fail(ctx, "net.fake", invoke, proposed.op, exc)
        if not safe:
            if final is not None and final.landed:
                landed_key = key if key is not None else final.key
                if landed_key is None:
                    landed_key = proposed.key
                ctx.record_effect(pending, final.op, final.scope, landed_key,
                                  final.irreversible, result(response) if result else None)
            else:
                info = final or proposed
                ctx.without_effect(pending, info.op, info.scope)
        return response


def _patch(stack, target, name, fn):
    original = getattr(target, name)
    setattr(target, name, functools.wraps(original)(fn))
    stack.callback(setattr, target, name, original)


def _block_network(stack, capture):
    def denied(*args, **kwargs):
        capture.reject("net", "shape.net: real network I/O is disabled during capture")
    for name in ("getaddrinfo", "getnameinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        _patch(stack, socket, name, denied)

    def internet_only(method):
        def guard(sock, *args, **kwargs):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                denied()
            return method(sock, *args, **kwargs)
        return guard

    for name in ("bind", "listen", "accept", "connect", "connect_ex", "send", "sendall", "sendto",
                 "sendmsg", "recv", "recv_into", "recvfrom", "recvfrom_into", "recvmsg",
                 "recvmsg_into"):
        if hasattr(socket.socket, name):
            _patch(stack, socket.socket, name, internet_only(getattr(socket.socket, name)))
    for name in ("send", "sendall", "write", "recv", "recv_into", "read", "do_handshake"):
        _patch(stack, ssl.SSLSocket, name, internet_only(getattr(ssl.SSLSocket, name)))


def _prepare():
    from . import _http, _mail

    # Warm optional imports in the runner so forks inherit them.
    _http.optional_clients()
    return _http, _mail


@contextlib.contextmanager
def _capture(workdir, fakes):
    http, mail = _prepare()
    capture = _Capture(workdir, fakes)
    with contextlib.ExitStack() as stack:
        http.install(stack, capture)
        mail.install(stack, capture)
        _block_network(stack, capture)
        yield


def _http_op(method, url):
    parsed = urlsplit(url)
    return parsed.hostname or "", f"{method} {parsed.hostname or ''}{parsed.path or '/'}"
