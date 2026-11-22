"""`sealed()`: make real sources of nondeterminism fail loudly during a run.

Inside a run every clock read, real sleep, `random` call, `uuid1/uuid4`, `os.urandom`, and
outbound socket connect raises `Escape`. A system under test that touches one is not
deterministic, so the run is invalid rather than merely flaky. `Escape` is a BaseException
so `except Exception` in the system cannot hide it, and `sealed()` also re-raises on exit if
any escape was swallowed anyway.

Not covered: C-level clock reads that bypass the patched names, such as `datetime.now()`.
"""

from __future__ import annotations

import os
import random
import socket
import time
import uuid
from contextlib import contextmanager
from typing import Any

_TIME = ["time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns",
         "process_time", "process_time_ns", "sleep"]


class Escape(BaseException):
    """The system under test touched real time, randomness, or the network."""


@contextmanager
def sealed():
    escapes: list[str] = []

    def blocked(name: str):
        def raiser(*args: Any, **kwargs: Any):
            escapes.append(name)
            raise Escape(f"{name} called inside a sealed run")

        return raiser

    patches: list[tuple[Any, str, Any]] = []

    def patch(owner: Any, name: str, label: str | None = None) -> None:
        patches.append((owner, name, getattr(owner, name)))
        setattr(owner, name, blocked(label or f"{getattr(owner, '__name__', 'socket')}.{name}"))

    for name in _TIME:
        patch(time, name)
    for name in random.__all__:
        fn = getattr(random, name)
        if getattr(random._inst, name, None) == fn:
            patch(random, name)
    patch(uuid, "uuid1")
    patch(uuid, "uuid4")
    patch(os, "urandom")
    patch(socket, "create_connection")
    patch(socket.socket, "connect", "socket.connect")
    patch(socket.socket, "connect_ex", "socket.connect_ex")
    try:
        yield escapes
    finally:
        for owner, name, original in reversed(patches):
            setattr(owner, name, original)
    if escapes:
        raise Escape(f"swallowed escape(s) inside a sealed run: {sorted(set(escapes))}")
