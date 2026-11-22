import pytest

from shape.choices import ExplicitChoices, SeededChoices
from shape.loop import Deadlock, SimLoop
from shape.sim.faults import ApiError
from shape.sim.tools import SimApprovals, SimDB, SimEmail, SimPayments
from shape.sim.world import World, digest
from shape.trace import Trace


def test_charge_same_key_same_params_returns_original():
    world = World()
    pay = SimPayments(world)
    first = pay.execute("charge", {"order": 1, "amount": 5}, "k")
    again = pay.execute("charge", {"order": 1, "amount": 5}, "k")
    assert first == again
    assert len(world.charges) == 1 and len(world.effects) == 1


def test_charge_same_key_different_params_is_an_error():
    pay = SimPayments(World())
    pay.execute("charge", {"order": 1, "amount": 5}, "k")
    with pytest.raises(ApiError) as err:
        pay.execute("charge", {"order": 1, "amount": 6}, "k")
    assert err.value.code == "idempotency_key_reuse"


def test_charge_without_key_or_with_new_key_charges_again():
    world = World()
    pay = SimPayments(world)
    pay.execute("charge", {"order": 1, "amount": 5}, None)
    pay.execute("charge", {"order": 1, "amount": 5}, None)
    pay.execute("charge", {"order": 1, "amount": 5}, "k2")
    assert len(world.charges) == 3


def test_refund_twice_fails_and_unknown_charge_fails():
    world = World()
    pay = SimPayments(world)
    charge = pay.execute("charge", {"order": 1, "amount": 5}, "k")["charge_id"]
    pay.execute("refund", {"charge_id": charge}, "r")
    with pytest.raises(ApiError) as twice:
        pay.execute("refund", {"charge_id": charge}, None)
    assert twice.value.code == "charge_already_refunded"
    with pytest.raises(ApiError):
        pay.execute("refund", {"charge_id": "ch_nope"}, None)
    # the same refund key replays the original answer instead of erroring
    assert pay.execute("refund", {"charge_id": charge}, "r") == {"refunded": charge}


def test_email_is_irreversible_and_idempotent_by_key():
    world = World()
    mail = SimEmail(world)
    mail.execute("send", {"order": 1, "to": "a"}, "k")
    mail.execute("send", {"order": 1, "to": "a"}, "k")
    assert len(world.outbox) == 1
    assert world.effects[0].reversible is False
    assert mail.specs[0].irreversible
    mail.execute("send", {"order": 1, "to": "a"}, None)
    assert len(world.outbox) == 2


def test_db_compare_and_set():
    world = World()
    world.seed_record("r", {"stock": 1})
    db = SimDB(world)
    got = db.execute("get", {"id": "r"}, None)
    db.execute("cas", {"id": "r", "expected": got["version"], "value": {"stock": 2}}, None)
    with pytest.raises(ApiError) as err:
        db.execute("cas", {"id": "r", "expected": got["version"], "value": {"stock": 3}}, None)
    assert err.value.code == "version_conflict"
    assert db.execute("get", {"id": "r"}, None)["value"] == {"stock": 2}
    assert world.before["r"] == {"stock": 1}


def test_every_fake_states_the_api_it_mirrors():
    for tool in (SimDB, SimPayments, SimEmail, SimApprovals):
        assert tool.api


def approve(realized, request="req"):
    world, trace = World(), Trace()
    approvals = SimApprovals(world, ExplicitChoices(realized), trace)

    async def go():
        return await approvals.request(request, "email", "send", {"order": 1})

    return world, go


def run(coro_fn):
    loop = SimLoop(ExplicitChoices({}), Trace())
    try:
        return loop.run_until_complete(coro_fn())
    finally:
        loop.close()


def test_approval_records_a_gate_for_the_exact_action():
    world, go = approve({})
    assert run(go) is True
    assert [g.action for g in world.gates] == [digest("email", "send", {"order": 1})]


def test_approval_reject_and_decision_is_stable_across_asks():
    world, go = approve({"human:req": 1})
    assert run(go) is False and run(go) is False
    assert world.gates == []


def test_approval_delay_answers_later_never_does_not():
    world, go = approve({"human:req": 2})
    assert run(go) is True

    world, go = approve({"human:req": 3})
    with pytest.raises(Deadlock):  # nobody will ever answer
        run(go)
    assert world.gates == []


def test_swarm_disables_kinds_without_touching_realized():
    seeded = SeededChoices(5)
    kinds = [f"k{i}" for i in range(20)]
    flags = [seeded.enabled(k) for k in kinds]
    assert any(flags) and not all(flags)
    assert seeded.realized == {}
