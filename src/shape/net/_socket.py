"""Block uncaptured Internet sockets and report where the call came from."""

from __future__ import annotations

import asyncio
import socket
import ssl
import sys
import sysconfig
from contextvars import ContextVar
from functools import cache

_STDLIB = sysconfig.get_path("stdlib") + "/"
_SHAPE = __name__.split(".", 1)[0]
_SOCKET_MODULES = {"asyncio", "concurrent", "socket", "ssl", "threading"}


@cache
def aiohttp_session():
    try:
        from aiohttp import ClientSession
    except ImportError:
        return None
    return ClientSession


def _origin():
    library = None
    frame = sys._getframe(1)
    while frame is not None:
        module = frame.f_globals.get("__name__", "").split(".", 1)[0]
        filename = frame.f_code.co_filename
        if (module != _SHAPE and module not in _SOCKET_MODULES
                and not filename.startswith("<")):
            if filename.startswith(_STDLIB) or "/site-packages/" in filename:
                library = library or module
            else:
                return library or "socket", f"{filename}:{frame.f_lineno}"
        frame = frame.f_back
    return library or "socket", "unknown call site"


def install(stack, capture, patch):
    request_origin = ContextVar("shape_network_request_origin", default=None)

    def deny(host=None, port=None):
        request = request_origin.get()
        if request is None:
            library, site = _origin()
        else:
            request_host, request_port, site = request
            host = request_host or host
            port = request_port if request_port is not None else port
            library = "aiohttp"
        endpoint = f"host {host or '<unknown>'!r}, port {port if port is not None else 'unknown'}"
        capture.reject(
            "net",
            f"shape.net: real network I/O is disabled during capture: {endpoint} via {library} "
            f"at {site}. Use a supported client and register a fake for the host with "
            "@shape.scenario(net=[...]), or omit net= for a live integration test.",
        )

    def address_denied(address, *args, **kwargs):
        if isinstance(address, tuple) and len(address) >= 2:
            deny(address[0], address[1])
        deny()

    def host_denied(host=None, port=None, *args, **kwargs):
        deny(host if host is not None else kwargs.get("hostname"), port)

    patch(stack, socket, "getnameinfo", address_denied)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        patch(stack, socket, name, host_denied)

    async def asyncio_denied(loop, host, port, **kwargs):
        deny(host, port)
    # Capture the caller before asyncio delegates DNS to a worker thread.
    patch(stack, asyncio.BaseEventLoop, "getaddrinfo", asyncio_denied)

    session = aiohttp_session()
    if session is not None:
        request = session._request

        async def track_request(client, method, str_or_url, *args, **kwargs):
            url = client._build_url(str_or_url)
            _, site = _origin()
            token = request_origin.set((url.host, url.port, site))
            try:
                return await request(client, method, str_or_url, *args, **kwargs)
            finally:
                request_origin.reset(token)

        patch(stack, session, "_request", track_request)

    def internet_only(name, method):
        def guard(sock, *args, **kwargs):
            if sock.family not in (socket.AF_INET, socket.AF_INET6):
                return method(sock, *args, **kwargs)
            address = kwargs.get("address")
            if args:
                if name in ("bind", "connect", "connect_ex"):
                    address = args[0]
                elif name == "sendto":
                    address = args[-1]
                elif name == "sendmsg" and len(args) >= 4:
                    address = args[3]
            if address is None:
                try:
                    address = sock.getpeername()
                except OSError:
                    address = sock.getsockname()
            address_denied(address)
        return guard

    for name in ("bind", "listen", "accept", "connect", "connect_ex", "send", "sendall", "sendto",
                 "sendmsg", "recv", "recv_into", "recvfrom", "recvfrom_into", "recvmsg",
                 "recvmsg_into"):
        if hasattr(socket.socket, name):
            patch(stack, socket.socket, name, internet_only(name, getattr(socket.socket, name)))
    for name in ("send", "sendall", "write", "recv", "recv_into", "read", "do_handshake"):
        patch(stack, ssl.SSLSocket, name, internet_only(name, getattr(ssl.SSLSocket, name)))
