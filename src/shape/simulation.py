"""Simulation: wires tools, faults, policy, runtime, and invariants, and runs one seed.

    sim = Simulation(seed=4127)
    sim.add_tool(SimDB); sim.add_tool(SimPayments); sim.add_tool(SimEmail)
    sim.add_invariant(...)
    result = sim.run(agent=make_agent(SAGAS["toy"]), policy=AdversarialPolicy(sim.choices, SPECS))

One Simulation runs once: its choice source accumulates the run's `realized` map.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .choices import ChoiceSource, ExplicitChoices, Realized, SeededChoices
from .invariants import ALL, Invariant, Outcome, Violation, evaluate
from .loop import Deadlock, SimLoop, TimeLimit
from .policies import AdversarialPolicy, Policy
from .runtime import SAGAS, SPECS, Runtime, Store, make_agent
from .seal import sealed
from .sim.faults import ACTOR_WEIGHT, Crash
from .sim.host import Host
from .sim.tools import SimApprovals, SimDB, SimEmail, SimPayments, Tool
from .sim.world import World
from .trace import Trace

Agent = Callable[[Store, Host, SimApprovals], Runtime]

ORDER = {"order": 17, "sku": "sku-1", "qty": 2, "amount": 50, "to": "ada@example.com"}
INVENTORY = {"inv:sku-1": {"stock": 10, "held": {}}}
ACTOR_TICKS = 40
ACTOR_PERIOD = 0.1
MAX_ROUNDS = 8


@dataclass
class RunResult:
    realized: Realized
    trace: Trace
    violations: list[Violation]
    world: World
    store: Store

    @property
    def ok(self) -> bool:
        return not self.violations


class Simulation:
    def __init__(self, seed: int | None = None, *, choices: ChoiceSource | None = None,
                 max_time: float = 10_000.0) -> None:
        assert (seed is None) != (choices is None), "give a seed or a choice source"
        self.choices: ChoiceSource = choices if choices is not None else SeededChoices(seed or 0)
        self.max_time = max_time
        self.tool_factories: list[Callable[[World], Tool]] = []
        self.invariants: list[Invariant] = []
        self.records: dict[str, dict[str, Any]] = {}

    def add_tool(self, factory: Callable[[World], Tool]) -> None:
        self.tool_factories.append(factory)

    def add_invariant(self, invariant: Invariant) -> None:
        self.invariants.append(invariant)

    def add_record(self, record_id: str, value: dict[str, Any]) -> None:
        self.records[record_id] = value

    def run(self, agent: Agent, policy: Policy, order: dict[str, Any] = ORDER) -> RunResult:
        trace = Trace()
        loop = SimLoop(self.choices, trace, max_time=self.max_time)
        world = World(now=loop.time)
        for record_id, value in self.records.items():
            world.seed_record(record_id, value)
        tools: dict[str, Tool] = {}
        for factory in self.tool_factories:
            tool = factory(world)
            tools[tool.name] = tool
        host = Host(tools, self.choices, trace)
        store = Store(host.crash_at)
        approver = SimApprovals(world, self.choices, trace)
        outcome = Outcome(world, store, trace)
        try:
            with sealed():
                loop.run_until_complete(
                    self._main(agent, policy, order, world, host, store, approver, outcome))
        except (Deadlock, TimeLimit) as stuck:
            outcome.stuck = str(stuck)
            trace.add("runtime", f"run stopped: {stuck}")
        finally:
            loop.close()
        violations = evaluate(self.invariants, outcome)
        return RunResult(dict(self.choices.realized), trace, violations, world, store)

    async def _main(self, agent: Agent, policy: Policy, order: dict[str, Any], world: World,
                    host: Host, store: Store, approver: SimApprovals, outcome: Outcome) -> None:
        trace, choices = outcome.trace, self.choices
        finished = False

        async def outsider() -> None:
            """An external actor that edits the first inventory record while the saga runs."""
            target = next(iter(world.records), None)
            for n in range(ACTOR_TICKS):
                await asyncio.sleep(ACTOR_PERIOD)
                if finished or target is None:
                    return
                on = choices.enabled("external_modification") * ACTOR_WEIGHT
                edit = choices.draw(f"actor:ext#{n}", [1.0, on, on])
                if edit:
                    delta = 5 if edit == 1 else -1
                    world.external_edit(target, delta)
                    trace.add("actor", f"external actor changes {target} stock by {delta:+d}  "
                              "[documented]", record=target, delta=delta)

        actor = asyncio.ensure_future(outsider())
        world.submitted.append(dict(order))  # the client hands the order over, once
        acked = False

        def ack(saga_id: str) -> None:
            nonlocal acked
            acked = True
            trace.add("client", f"order {order['order']} acknowledged as {saga_id}")

        for attempt in range(MAX_ROUNDS):
            runtime = agent(store, host, approver)
            try:
                if attempt == 0:
                    await runtime.run(order, policy, ack)
                else:
                    await runtime.recover(policy)
                    if not acked:  # no ack ever arrived, so the client submits again
                        trace.add("client", f"no ack for order {order['order']}; resubmitting")
                        await runtime.run(order, policy, ack)
                if acked and not store.open_sagas():
                    break
            except Crash:
                trace.add("runtime", "process restarts; recover() on the durable store")
        finished = True
        actor.cancel()
        await asyncio.gather(actor, return_exceptions=True)
        host.faults_on = False
        before = world.snapshot()
        await agent(store, host, approver).compensate_again()
        if world.snapshot() != before:
            outcome.recompensation = "running the compensations again changed the world"


def place_order(saga: str, choices: ChoiceSource, agent: Agent | None = None,
                invariants: list[Invariant] | None = None,
                policy: Callable[[ChoiceSource], Policy] | None = None) -> RunResult:
    """One run of the place-order scenario against saga `toy` or `fixed`.

    `policy` builds the agent's policy from the run's choices; the default is adversarial.
    """
    sim = Simulation(choices=choices)
    for tool in (SimDB, SimPayments, SimEmail):
        sim.add_tool(tool)
    for inv in invariants or ALL:
        sim.add_invariant(inv)
    for record_id, value in INVENTORY.items():
        sim.add_record(record_id, value)
    made = policy(sim.choices) if policy else AdversarialPolicy(sim.choices, SPECS)
    return sim.run(agent or make_agent(SAGAS[saga]), made)


def run_seed(saga: str, seed: int) -> RunResult:
    return place_order(saga, SeededChoices(seed))


def replay(saga: str, realized: Realized) -> RunResult:
    return place_order(saga, ExplicitChoices(realized))
