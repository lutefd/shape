"""A deterministic asyncio event loop with a virtual clock.

* Time is virtual. `time()` returns the loop's own clock; when nothing is ready the clock
  jumps to the next timer, so `asyncio.sleep(3600)` costs no wall time.
* Which ready callback runs next is a choice: `sched#<n>` is drawn whenever two or more
  callbacks are ready. 0 is FIFO (the nominal order); k runs the k-th ready callback first.
* Nothing runnable, no timers, and the main task unfinished is a `Deadlock`. Running past
  `max_time` virtual seconds is a `TimeLimit` (a livelock in practice).

The loop never reads a real clock, so it works inside `sealed()`.
"""

from __future__ import annotations

import asyncio
import heapq
from typing import Any

from .choices import ChoiceSource
from .trace import Trace


class Deadlock(Exception):
    """Nothing is runnable and nothing is scheduled, but the main task has not finished."""


class TimeLimit(Exception):
    """Virtual time passed `max_time` without the main task finishing."""


class SimLoop(asyncio.BaseEventLoop):
    def __init__(self, choices: ChoiceSource, trace: Trace, *, max_time: float = 10_000.0) -> None:
        super().__init__()
        self._now = 0.0
        self._choices = choices
        self._trace = trace
        self._max_time = max_time
        self._picks = 0
        self._reorder = choices.enabled("sched")
        self.errors: list[dict[str, Any]] = []
        trace.clock = self.time

    def time(self) -> float:
        return self._now

    def _run_once(self) -> None:
        scheduled = self._scheduled
        while scheduled and scheduled[0]._cancelled:
            self._timer_cancelled_count -= 1
            heapq.heappop(scheduled)._scheduled = False
        if not self._ready and not self._stopping:
            if not scheduled:
                raise Deadlock("nothing runnable, no timers, main task unfinished")
            self._now = max(self._now, scheduled[0]._when)
            if self._now > self._max_time:
                raise TimeLimit(f"virtual time passed {self._max_time}")
        while scheduled and scheduled[0]._when <= self._now:
            handle = heapq.heappop(scheduled)
            handle._scheduled = False
            self._ready.append(handle)
        ready = self._ready
        while ready and ready[0]._cancelled:
            ready.popleft()
        if not ready:
            return
        index = 0
        if len(ready) > 1:
            n = len(ready)
            weights = [1.0] + [0.1 / (n - 1) if self._reorder else 0.0] * (n - 1)
            index = self._choices.draw(f"sched#{self._picks}", weights)
            self._picks += 1
            if index:
                self._trace.add("sched", f"ran ready callback {index} of {n} first", index=index)
        if index:
            ready.rotate(-index)
            handle = ready.popleft()
            ready.rotate(index)
        else:
            handle = ready.popleft()
        if not handle._cancelled:
            handle._run()

    def call_exception_handler(self, context: dict[str, Any]) -> None:
        # The default handler logs, and logging reads the real clock.
        self.errors.append(context)

    def _write_to_self(self) -> None:
        raise RuntimeError("SimLoop has no threads")
