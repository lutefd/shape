"""Every invariant must fail on a deliberately broken variant, or it proves nothing.

Each test runs a mutant (a Runtime subclass with one defect) or a tampered World through the
same scenario the real runtime passes, and checks the one expected invariant fires.
"""

import asyncio

from shape.choices import ExplicitChoices
from shape.invariants import ALL, Outcome, at_most_once_effect, gate_before_irreversible
from shape.runtime import SAGAS, Runtime
from shape.runtime.store import Intent
from shape.sim.world import World
from shape.simulation import place_order
from shape.trace import Trace

ABORT_AFTER_CHARGE = {"agent#2": 6}
EXTERNAL_RESTOCK = {"actor:ext#3": 1}
CRASH_BEFORE_CHARGE = {"crash:call:payments.charge#0": 1}


def run(realized, runtime_cls=Runtime, saga="fixed"):
    config = SAGAS[saga]
    return place_order(saga, ExplicitChoices(realized),
                       agent=lambda store, host, approver: runtime_cls(store, host, approver, config))


def fired(result):
    return {v.invariant for v in result.violations}


def test_real_runtime_passes_every_scenario_the_mutants_fail():
    for realized in ({}, ABORT_AFTER_CHARGE, {**ABORT_AFTER_CHARGE, **EXTERNAL_RESTOCK},
                     CRASH_BEFORE_CHARGE, {"fault:payments.charge#0": 3}):
        assert run(realized).ok, realized


def test_there_are_six_invariants():
    assert [i.name for i in ALL] == [
        "at_most_once_effect", "gate_before_irreversible", "rollback_restores_state",
        "no_invisible_effects", "compensation_idempotent", "saga_terminates"]


# 1. at_most_once_effect
def test_at_most_once_fails_when_a_retry_mints_a_new_key():
    broken = run({"fault:payments.charge#0": 3}, saga="toy")
    assert fired(broken) == {"at_most_once_effect"}
    assert "2 payments.charge effects" in broken.violations[0].message


def test_at_most_once_fails_on_a_tampered_world():
    world = World()
    for _ in range(2):
        world.effect("email", "send", "order:1/email", None, False, {"order": 1})
    assert at_most_once_effect(Outcome(world, None, Trace()))


# 2. gate_before_irreversible
class NoGate(Runtime):
    async def _gate(self, saga_id, tool, op, args):
        return True


def test_gate_fails_when_the_runtime_skips_approval():
    assert fired(run({}, NoGate)) == {"gate_before_irreversible"}


def test_gate_fails_when_the_approval_was_for_a_different_action():
    class WrongAction(Runtime):
        async def _gate(self, saga_id, tool, op, args):
            return await super()._gate(saga_id, tool, op, {**args, "to": "someone-else"})

    assert fired(run({}, WrongAction)) == {"gate_before_irreversible"}


def test_gate_fails_when_approval_comes_after_the_effect():
    world = World()
    world.effect("email", "send", "order:1/email", None, False, {"order": 1})
    from shape.sim.world import Gate, digest

    world.gates.append(Gate("r", digest("email", "send", {"order": 1}), after_effects=1))
    assert gate_before_irreversible(Outcome(world, None, Trace()))


# 3. rollback_restores_state
class NeverReleases(Runtime):
    async def _release(self, saga_id, force):
        return True


class NeverRefunds(Runtime):
    async def _refund(self, saga_id, charge, force):
        return True


class BlindRelease(Runtime):
    """Overwrites the record with the value it had at the start instead of applying a delta."""

    async def _release(self, saga_id, force):
        got = await self._read("inv:sku-1")
        args = {"id": "inv:sku-1", "expected": got["version"],
                "value": {"stock": 10, "held": {}}}
        self.store.put_intent(Intent(saga_id, "comp:reserve", "db", "cas", args, None))
        await self._call_step(saga_id, "comp:reserve", compensating=True)
        return True


def test_rollback_fails_when_inventory_is_not_released():
    assert "rollback_restores_state" in fired(run(ABORT_AFTER_CHARGE, NeverReleases))


def test_rollback_fails_when_the_charge_is_not_refunded():
    assert "rollback_restores_state" in fired(run(ABORT_AFTER_CHARGE, NeverRefunds))


def test_rollback_fails_when_release_overwrites_an_external_change():
    realized = {**ABORT_AFTER_CHARGE, **EXTERNAL_RESTOCK}
    assert "rollback_restores_state" in fired(run(realized, BlindRelease))
    assert run(realized).ok  # the real runtime applies a delta and keeps the restock


def test_rollback_fails_when_a_compensated_saga_sent_an_email():
    broken = run({}, NoGate)  # committed saga with an email: flip its state to compensated
    broken.store.sagas["saga-17"].state = "compensated"
    from shape.invariants import rollback_restores_state

    messages = rollback_restores_state(Outcome(broken.world, broken.store, broken.trace))
    assert any("cannot be undone" in m for m in messages)


# 4. no_invisible_effects
class SilentRefunds(Runtime):
    async def _audit_call(self, saga_id, intent):
        if intent.op != "refund":
            await super()._audit_call(saga_id, intent)


def test_invisible_effects_fail_when_a_call_is_not_audited():
    assert fired(run(ABORT_AFTER_CHARGE, SilentRefunds)) == {"no_invisible_effects"}


# 5. compensation_idempotent
class DoubleRelease(Runtime):
    """Releases without checking the hold is still there, so running it again over-restocks."""

    async def _release(self, saga_id, force):
        got = await self._read("inv:sku-1")
        value = got["value"]
        args = {"id": "inv:sku-1", "expected": got["version"],
                "value": {"stock": value["stock"] + 2, "held": {}}}
        self.store.put_intent(Intent(saga_id, "comp:reserve", "db", "cas", args, None))
        await self._call_step(saga_id, "comp:reserve", compensating=True)
        return True


def test_compensation_idempotent_fails_when_rerunning_changes_the_world():
    assert "compensation_idempotent" in fired(run(ABORT_AFTER_CHARGE, DoubleRelease))


# 6. saga_terminates
class ForgetsToRecover(Runtime):
    async def recover(self, policy):
        return None


class WaitsForeverForApproval(Runtime):
    async def _gate(self, saga_id, tool, op, args):
        await asyncio.get_running_loop().create_future()


def test_terminates_fails_when_recovery_does_nothing_after_a_crash():
    broken = run(CRASH_BEFORE_CHARGE, ForgetsToRecover)
    assert fired(broken) == {"saga_terminates"}
    assert "running" in broken.violations[0].message


def test_terminates_fails_on_deadlock():
    broken = run({}, WaitsForeverForApproval)
    assert fired(broken) == {"saga_terminates"}
    assert "did not finish" in broken.violations[0].message
