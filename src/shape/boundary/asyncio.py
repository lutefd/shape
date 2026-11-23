"""The boundary event loop, with real executor work and virtual asyncio timers."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from ..loop import SimLoop
from ..trace import Trace

if TYPE_CHECKING:
    from .api import _Context


class BoundaryLoop(SimLoop):
    def __init__(self, ctx: _Context) -> None:
        super().__init__(ctx.choices, Trace(), max_time=float("inf"))
        self.ctx = ctx
        self.wakeup = threading.Event()
        self.external_work = 0
        self.pools: set[ThreadPoolExecutor] = set()

    def _pick_ready(self) -> int:
        return self.ctx.pick_task(self._ready)

    def _write_to_self(self) -> None:
        self.wakeup.set()

    def _on_idle(self) -> None:
        self.wakeup.clear()
        if self._ready:
            return
        if self.external_work or self._user_threads_alive():
            # Recheck, since a thread can exit without posting back to the loop.
            self.wakeup.wait(0.05)
        elif not self._ready:
            super()._on_idle()

    def _user_threads_alive(self) -> bool:
        current = threading.current_thread()
        return any(thread not in (current, self.ctx.watchdog)
                   and all(thread not in pool._threads for pool in self.pools)
                   for thread in threading.enumerate())

    def _work_done(self, _) -> None:
        self.external_work -= 1

    def run_in_executor(self, executor, func, *args):
        future = super().run_in_executor(executor, func, *args)
        pool = self._default_executor if executor is None else executor
        if isinstance(pool, ThreadPoolExecutor):
            self.pools.add(pool)
        self.external_work += 1
        future.add_done_callback(self._work_done)
        return future

    async def shutdown_default_executor(self, timeout=None) -> None:
        self.external_work += 1
        try:
            await super().shutdown_default_executor(timeout)
        finally:
            self.external_work -= 1
