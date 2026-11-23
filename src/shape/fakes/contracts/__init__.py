"""Run the same provider contract probes against a fake and its test service."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, build_opener
from urllib.request import Request as UrlRequest
from uuid import uuid4

from .._util import header
from ..base import Request, Response

_MISSING = object()


@dataclass(frozen=True)
class Observation:
    status: int
    code: str | int | None
    shape: dict[str, str]


@dataclass
class ContractResult:
    name: str
    observations: dict[str, Observation]
    differences: list[str] = field(default_factory=list)
    skipped: str | None = None
    verified_on: str | None = None

    @property
    def passed(self):
        return not self.differences and self.skipped is None


class ContractFailure(Exception):
    """A probe cannot continue without a preceding response or fact."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _live_send(req):
    outbound = UrlRequest(req.url, data=req.body or None, headers=dict(req.headers),
                          method=req.method)
    try:
        response = build_opener(_NoRedirect).open(outbound, timeout=20)
    except HTTPError as error:
        response = error
    except (URLError, OSError, ValueError) as error:
        raise ContractFailure(f"transport failed ({type(error).__name__})") from None
    with response:
        try:
            return Response(response.code, dict(response.headers), response.read())
        except OSError as error:
            raise ContractFailure(f"response read failed ({type(error).__name__})") from None


def _get(body, path):
    value = body
    for key in path.split("."):
        if isinstance(value, dict):
            value = value.get(key, _MISSING)
        elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        else:
            return _MISSING
    return value


def _kind(value):
    return "missing" if value is _MISSING else type(value).__name__


class Probe:
    def __init__(self, send, base, headers, marker):
        self.send = send
        self.base = base
        self.headers = headers
        self.marker = marker
        self.observations = {}
        self.differences = []

    def call(self, step, method, path, status, *, fields=(), code_path=None, code=None,
             body=None, form=None, query=None, key=None):
        data, headers = b"", dict(self.headers)
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if form is not None:
            data = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if key is not None:
            headers["Idempotency-Key"] = key
        if query is not None:
            path = f"{path}?{urlencode(query, doseq=True)}"
        response = self.send(Request(method, self.base + path, headers, data, body))
        value = None
        content = "empty"
        if response.body:
            try:
                value = json.loads(response.body)
                content = _kind(value)
            except (ValueError, UnicodeError):
                content = "sse" if (header(response.headers, "Content-Type") or "").startswith(
                    "text/event-stream"
                ) else "bytes"
        found_code = _get(value, code_path) if code_path else None
        found_code = found_code if type(found_code) in (str, int) else None
        shape = {"body": content, **{name: _kind(_get(value, name)) for name in fields}}
        self.observations[step] = Observation(response.status, found_code, shape)
        if response.status != status:
            self.differences.append(f"{step}: expected HTTP {status}, got {response.status}")
        elif code_path and found_code != code:
            self.differences.append(f"{step}: expected error code {code!r}, got {found_code!r}")
        elif any(kind == "missing" for kind in shape.values()):
            self.differences.append(f"{step}: missing fields in {shape}")
        return value

    def check(self, step, condition):
        self.observations[step] = Observation(0, None, {"assertion": "pass" if condition else "fail"})
        if not condition:
            self.differences.append(f"{step}: assertion failed")

    @staticmethod
    def require(value, path):
        result = _get(value, path)
        if result is _MISSING or result is None:
            raise ContractFailure(f"prerequisite response omitted {path}")
        return result

    def cleanup(self, method, path, *, body=None):
        headers = dict(self.headers)
        raw = b""
        if body is not None:
            headers["Content-Type"] = "application/json"
            raw = json.dumps(body).encode()
        try:
            response = self.send(Request(method, self.base + path, headers, raw, body))
        except ContractFailure as error:
            self.differences.append(f"cleanup: {error}")
            return
        if not 200 <= response.status < 300:
            self.differences.append(f"cleanup: HTTP {response.status}")


def run_contract(name: str, *, live: bool = False, env=None) -> ContractResult:
    """Return safe-to-print status, selected response field types, and differences."""
    from ._suites import CONTRACTS

    suite = CONTRACTS[name]
    marker = uuid4().hex
    env = os.environ if env is None else env
    if live:
        missing = [name for name in suite.settings if not env.get(name)]
        if missing:
            return ContractResult(name, {}, skipped="missing " + ", ".join(missing))
    fake_settings = dict(suite.settings)
    fake = suite.fake()
    state: dict[str, Any] = {}
    probe = Probe(lambda req: fake.handle(req, state), *suite.connect(fake_settings, False), marker)
    try:
        suite.script(probe, fake_settings, False)
    except ContractFailure as error:
        probe.differences.append(str(error))
    if not live or probe.differences:
        return ContractResult(name, probe.observations, probe.differences)
    try:
        real = Probe(_live_send, *suite.connect(env, True), marker)
    except ContractFailure as error:
        return ContractResult(name, {}, [str(error)])
    try:
        suite.script(real, env, True)
    except ContractFailure as error:
        real.differences.append(str(error))
    delta = list(real.differences)
    for step in sorted(probe.observations.keys() | real.observations.keys()):
        expected, actual = probe.observations.get(step), real.observations.get(step)
        if expected != actual:
            delta.append(f"{step}: fake={expected!r}, live={actual!r}")
    date = datetime.now(UTC).date().isoformat() if not delta else None
    return ContractResult(name, real.observations, delta, verified_on=date)


def assert_contract(name: str, *, live: bool = False, env=None) -> ContractResult:
    """Pytest helper. Fail on contract differences and skip for missing live settings."""
    result = run_contract(name, live=live, env=env)
    if result.skipped:
        import pytest

        pytest.skip(result.skipped)
    assert result.passed, "\n".join(result.differences)
    return result


def command(args):
    result = run_contract(args.name, live=args.live)
    if args.report:
        Path(args.report).write_text(json.dumps(asdict(result), indent=2) + "\n")
    if result.skipped:
        print(f"SKIP {result.name}: {result.skipped}")
        return 0
    if result.differences:
        print(f"FAIL {result.name}\n" + "\n".join(f"  {line}" for line in result.differences))
        return 1
    mode = f"live comparison on {result.verified_on}" if args.live else "fake"
    print(f"PASS {result.name} ({mode}, {len(result.observations)} observations)")
    return 0
