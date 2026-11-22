"""SimApprovals: a simulated human who may approve, reject, delay, or never answer.

The decision for a request is drawn once at `human:<request>` and then fixed, so asking again
after a crash returns the same answer, as a real pending request would. Approving records a
`Gate` in the world naming the exact action approved. This is the one fake that is not a
mirror of an external API.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ...choices import ChoiceSource
from ...trace import Trace
from ..world import Gate, World, digest

APPROVE, REJECT, DELAY, NEVER = range(4)
NAMES = ("approve", "reject", "delay", "never")
DELAY_SECONDS = 30.0


class SimApprovals:
    name = "approvals"
    api = "human-in-the-loop approval request (no external API)"
    specs = ()

    def __init__(self, world: World, choices: ChoiceSource, trace: Trace) -> None:
        self.world, self.choices, self.trace = world, choices, trace
        self._decided: dict[str, tuple[int, float]] = {}

    async def request(self, request: str, tool: str, op: str, args: dict[str, Any]) -> bool:
        loop = asyncio.get_running_loop()
        if request not in self._decided:
            on = self.choices.enabled("human")
            weights = [1.0, 0.05 * on, 0.08 * on, 0.03 * on]
            outcome = self.choices.draw(f"human:{request}", weights)
            self._decided[request] = (outcome, loop.time())
            self.trace.add("human", f"{NAMES[outcome]} {request}", request=request, outcome=outcome)
        outcome, asked_at = self._decided[request]
        if outcome == REJECT:
            return False
        if outcome == NEVER:
            await loop.create_future()
        if outcome == DELAY:
            await asyncio.sleep(max(0.0, asked_at + DELAY_SECONDS - loop.time()))
        action = digest(tool, op, args)
        if not any(g.request == request for g in self.world.gates):
            self.world.gates.append(Gate(request, action, len(self.world.effects)))
        return True
