"""Host: where the runtime's tool calls meet the simulated world.

Every call goes through one pipeline, and every step of it is an addressable choice:

1. `crash:call:<tool>.<op>#<n>`  1 = the process dies before the call, 2 = after the effect
   lands but before the caller sees the result.
2. `fault:<tool>.<op>#<n>`       which fault, if any, hits this call (see faults.py).
3. execute against the fake, which applies real effects to the World.

`n` counts calls to that op over the whole run, across crashes. Durable writes get the same
crash treatment through `crash_at`, with keys `crash:durable:<table>#<n>`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..choices import ChoiceSource
from ..trace import Trace
from .faults import (
    BURST_LENGTHS,
    BURSTS,
    CALL_CRASH_WEIGHT,
    CALL_FAULTS,
    CRASH_WEIGHT,
    MAX_CRASHES,
    PROVENANCE,
    ApiError,
    Crash,
    RateLimited,
    ServerError,
    ToolTimeout,
    call_weights,
)
from .tools import Tool

CALL_LATENCY = 0.1
CALL_TIMEOUT = 2.0


def render_args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in args.items() if k != "value")


class Host:
    def __init__(self, tools: dict[str, Tool], choices: ChoiceSource, trace: Trace) -> None:
        self.tools, self.choices, self.trace = tools, choices, trace
        self.crashes = 0
        self.faults_on = True
        self._counts: dict[str, int] = {}
        self._bursts: dict[str, tuple[str, int]] = {}  # op -> (burst fault, calls it still hits)

    def _next(self, name: str) -> int:
        n = self._counts.get(name, 0)
        self._counts[name] = n + 1
        return n

    def crash_at(self, point: str, weight: float = CRASH_WEIGHT) -> int:
        """Draw whether the process dies at `point`. Returns 0, 1 (before) or 2 (after)."""
        if not self.faults_on or self.crashes >= MAX_CRASHES:
            return 0
        n = self._next(f"crash:{point}")
        on = weight * self.choices.enabled("crash")
        mode = self.choices.draw(f"crash:{point}#{n}", [1.0, on, on])
        if mode:
            self.crashes += 1
            where = "before" if mode == 1 else "after"
            self.trace.add("crash", f"agent process dies {where} {point}", point=point, mode=mode)
        return mode

    async def call(self, tool: str, op: str, args: dict[str, Any], key: str | None = None,
                   compensating: bool = False) -> dict[str, Any]:
        name = f"{tool}.{op}"
        self.trace.add("call", f"{name}({render_args(args)})  key={key}", tool=tool, op=op, key=key)
        crash = self.crash_at(f"call:{name}", CALL_CRASH_WEIGHT)
        if crash == 1:
            raise Crash(f"before {name}")
        fault = ""
        if self.faults_on:
            n = self._next(f"fault:{name}")
            burst, left = self._bursts.pop(name, ("", 0))
            if left:
                # Inside a burst: the earlier draw decided this call, so nothing is drawn here.
                kind = BURSTS[burst]
                fault = kind
                if left > 1:
                    self._bursts[name] = (burst, left - 1)
                self.trace.add("fault", f"{kind} on {name}  [{PROVENANCE[burst]}, burst continues]",
                               fault=burst, tool=tool, op=op)
            else:
                weights = call_weights(self.choices.enabled, compensating)
                drawn = self.choices.draw(f"fault:{name}#{n}", weights)
                if drawn:
                    fault = CALL_FAULTS[drawn - 1]
                    note = ""
                    if fault in BURSTS:
                        size = BURST_LENGTHS[self.choices.draw(
                            f"burst:{name}#{n}", [1.0] * len(BURST_LENGTHS))]
                        if size > 1:
                            self._bursts[name] = (fault, size - 1)
                        note = f", {size} calls"
                    self.trace.add("fault", f"{fault} on {name}  [{PROVENANCE[fault]}{note}]",
                                   fault=fault, tool=tool, op=op)
                    fault = BURSTS.get(fault, fault)
        await asyncio.sleep(CALL_LATENCY)
        if fault == "latency":
            jitter = self.choices.draw(f"jitter:latency#{self._next('jitter:latency')}", [1, 1, 1])
            await asyncio.sleep(0.3 * (jitter + 1))
        elif fault == "timeout":
            await asyncio.sleep(CALL_TIMEOUT)
            raise ToolTimeout(name)
        elif fault == "rate_limit":
            raise RateLimited(1.0)
        elif fault == "transient_5xx":
            raise ServerError(name)
        elif fault == "compensation_failure":
            raise ApiError("processing_error")
        result = self.tools[tool].execute(op, args, key)
        if fault == "duplicate_delivery":
            try:
                self.tools[tool].execute(op, args, key)
            except ApiError:
                pass
        if crash == 2:
            raise Crash(f"after effect of {name}")
        if fault == "timeout_after_commit":
            await asyncio.sleep(CALL_TIMEOUT)
            raise ToolTimeout(name)
        return result
