"""Each fault kind, driven by an explicit choice, against the fixed runtime and the world."""

import pytest

from shape.sim.faults import CALL_FAULTS, FAULTS, PROVENANCE, Crash
from shape.simulation import replay

ABORT = {"agent#2": 6}


def index(kind):
    return CALL_FAULTS.index(kind) + 1


def states(result):
    return [s.state for s in result.store.sagas.values()]


def test_provenance_is_declared_for_every_fault_kind():
    assert {f.provenance for f in FAULTS} <= {"documented", "observed", "theoretical"}
    for kind in (*CALL_FAULTS, "external_modification", "crash"):
        assert kind in PROVENANCE
    assert {"latency", "timeout", "timeout_after_commit", "duplicate_delivery", "rate_limit",
            "transient_5xx", "compensation_failure"} == set(CALL_FAULTS)


def test_crash_cannot_be_swallowed_by_except_exception():
    assert issubclass(Crash, BaseException) and not issubclass(Crash, Exception)


def test_timeout_before_commit_leaves_no_effect_and_the_retry_succeeds():
    result = replay("fixed", {"fault:payments.charge#0": index("timeout")})
    assert result.ok and states(result) == ["committed"]
    assert len(result.world.charges) == 1
    assert any(e.kind == "retry" for e in result.trace.events)


def test_timeout_after_commit_lands_the_effect_and_stable_key_dedupes_the_retry():
    result = replay("fixed", {"fault:payments.charge#0": index("timeout_after_commit")})
    assert result.ok and states(result) == ["committed"]
    assert len(result.world.charges) == 1
    keys = {e.data["key"] for e in result.trace.events if e.kind in ("call", "retry")
            and e.data["op"] == "charge"}
    assert keys == {"saga-17:charge"}


def test_duplicate_delivery_is_absorbed_by_idempotency_keys():
    for op in ("charge", "send"):
        tool = "payments" if op == "charge" else "email"
        result = replay("fixed", {f"fault:{tool}.{op}#0": index("duplicate_delivery")})
        assert result.ok
        assert len(result.world.charges) == 1 and len(result.world.outbox) == 1


@pytest.mark.parametrize("kind", ["latency", "rate_limit", "transient_5xx"])
def test_benign_faults_do_not_change_the_outcome(kind):
    result = replay("fixed", {"fault:payments.charge#0": index(kind),
                              "fault:db.cas#0": index(kind)})
    assert result.ok and states(result) == ["committed"]


def test_compensation_failure_is_retried_until_the_refund_lands():
    result = replay("fixed", {**ABORT, "fault:payments.refund#0": index("compensation_failure")})
    assert result.ok and states(result) == ["compensated"]
    assert all(c.refunded for c in result.world.charges.values())


def test_compensation_failure_every_time_escalates_and_says_so():
    always = {f"fault:payments.refund#{n}": index("compensation_failure") for n in range(8)}
    result = replay("fixed", {**ABORT, **always})
    assert result.ok and states(result) == ["escalated"]
    assert any(a.get("kind") == "escalate" for a in result.store.audit_log)


def test_external_modification_is_kept_by_conflict_aware_release():
    result = replay("fixed", {**ABORT, "actor:ext#3": 1})
    assert result.ok and states(result) == ["compensated"]
    assert result.world.records["inv:sku-1"].value == {"stock": 15, "held": {}}


def test_crash_before_a_call_recovers_and_finishes():
    result = replay("fixed", {"crash:call:payments.charge#0": 1})
    assert result.ok and states(result) == ["committed"] and len(result.world.charges) == 1
    assert any(e.kind == "crash" for e in result.trace.events)


def test_crash_after_the_effect_before_the_return_recovers_without_a_second_effect():
    result = replay("fixed", {"crash:call:payments.charge#0": 2})
    assert result.ok and states(result) == ["committed"] and len(result.world.charges) == 1
    result = replay("fixed", {"crash:call:email.send#0": 2})
    assert result.ok and len(result.world.outbox) == 1


@pytest.mark.parametrize("mode", [1, 2])
def test_crash_at_every_durable_write_recovers(mode):
    clean = replay("fixed", {})
    writes = sum(1 for _ in clean.store.audit_log) + 10  # generous upper bound on writes
    for table in ("sagas", "intents", "audit"):
        for n in range(writes):
            result = replay("fixed", {f"crash:durable:{table}#{n}": mode})
            assert result.ok, (table, n, mode, [v.message for v in result.violations])
