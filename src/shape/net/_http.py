"""Translate supported HTTP transports to the fake request and response protocol."""

from __future__ import annotations

import functools
import http.client
import importlib
import io
import json
from types import SimpleNamespace
from urllib import error, request
from urllib.parse import parse_qsl, urlsplit
from urllib.response import addinfourl

from ..boundary.api import _async_point
from ..fakes.base import Request, Response
from . import _Headers, _http_op, _patch

_SAFE = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_STATUS_FAULTS = {"rate_limit": 429, "server_error": 503, "client_error": 400}


class _HTTPStatus(Exception):
    def __init__(self, status):
        self.status = status
        self.__shape_fault_label__ = f"HTTP {status}"


@functools.cache
def optional_clients():
    clients = {}
    for name in ("requests", "httpx"):
        try:
            clients[name] = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
    return clients


def _bytes(body):
    if body is None:
        return b""
    if isinstance(body, str):
        return body.encode()
    if hasattr(body, "read"):
        return _bytes(body.read())
    if isinstance(body, (bytes, bytearray, memoryview)):
        return bytes(body)
    return b"".join(_bytes(part) for part in body)


def _json(body):
    try:
        return json.loads(body)
    except (ValueError, UnicodeError):
        return None


def _result(response):
    return _json(response.body)


def _send(capture, method, url, headers, body, errors):
    host, op = _http_op(method, url)
    provider = capture.provider(host, "handle")
    headers = _Headers(headers)
    body = _bytes(body)
    content_type = headers.get("Content-Type", "").partition(";")[0].strip().lower()
    payload = _json(body) if content_type.endswith(("/json", "+json")) else None
    req = Request(method, url, headers, body, payload)
    args = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    if isinstance(payload, dict):
        args.update(payload)
    elif content_type == "application/x-www-form-urlencoded":
        args.update(parse_qsl(body.decode(errors="replace"), keep_blank_values=True))
    key = headers.get("Idempotency-Key")
    if method not in _SAFE:
        args["Idempotency-Key"] = key
    try:
        return capture.perform(
            req, op, args, provider, lambda fake, state: fake.handle(req, state),
            errors, safe=method in _SAFE, key=key, result=_result,
        )
    except _HTTPStatus as exc:
        return Response(exc.status, {"Content-Type": "application/json"},
                        json.dumps({"errors": ["shape: injected HTTP " + str(exc.status)]}).encode())


def _errors(timeout, reset, *, timeout_after=None):
    return {
        "timeout": lambda: timeout("shape: injected timeout"),
        "timeout_after_commit": lambda: (timeout_after or timeout)("shape: injected read timeout"),
        "connection_reset_after_commit": lambda: reset("shape: injected connection reset"),
        **{name: functools.partial(_HTTPStatus, code) for name, code in _STATUS_FAULTS.items()},
    }


def _messages(headers):
    msg = http.client.HTTPMessage()
    for key, value in headers.items():
        msg[key] = value
    return msg


def install(stack, capture):
    def urllib_open(handler, http_class, req, **kwargs):
        response = _send(
            capture, req.get_method(), req.full_url, dict(req.header_items()), req.data,
            _errors(lambda msg: error.URLError(TimeoutError(msg)), ConnectionResetError,
                    timeout_after=TimeoutError),
        )
        wrapped = addinfourl(io.BytesIO(response.body), _messages(response.headers),
                             req.full_url, response.status)
        wrapped.msg = http.client.responses.get(response.status, "")
        return wrapped

    _patch(stack, request.AbstractHTTPHandler, "do_open", urllib_open)

    clients = optional_clients()
    if requests := clients.get("requests"):
        def requests_send(adapter, req, *args, **kwargs):
            from urllib3.response import HTTPResponse

            response = _send(
                capture, req.method, req.url, req.headers, req.body,
                _errors(functools.partial(requests.ReadTimeout, request=req),
                        functools.partial(requests.ConnectionError, request=req)),
            )
            body = io.BytesIO(response.body)
            raw = HTTPResponse(
                body=body, headers=response.headers, status=response.status,
                reason=http.client.responses.get(response.status, ""), preload_content=False,
                original_response=SimpleNamespace(msg=_messages(response.headers),
                                                  isclosed=lambda: body.closed, close=body.close),
            )
            return adapter.build_response(req, raw)

        _patch(stack, requests.adapters.HTTPAdapter, "send", requests_send)

    if httpx := clients.get("httpx"):
        def httpx_response(req, body):
            response = _send(
                capture, req.method, str(req.url), req.headers, body,
                _errors(functools.partial(httpx.ReadTimeout, request=req),
                        functools.partial(httpx.ReadError, request=req)),
            )
            return httpx.Response(response.status, headers=response.headers,
                                  stream=httpx.ByteStream(response.body))

        def httpx_send(transport, req):
            return httpx_response(req, req.read())

        async def httpx_async_send(transport, req):
            _, label = _http_op(req.method, str(req.url))
            await _async_point(label)
            return httpx_response(req, await req.aread())

        _patch(stack, httpx.HTTPTransport, "handle_request", httpx_send)
        _patch(stack, httpx.AsyncHTTPTransport, "handle_async_request", httpx_async_send)
