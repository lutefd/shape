"""Crash recovery: an intent a crash cut off is in doubt, and the pivot never compensates.

These replay the TLC counterexamples (verification/tla) and the sweep's shrunk finding through
ExplicitChoices, and check each against a runtime mutant that lacks the fix.
"""

from shape.choices import ExplicitChoices
from shape.runtime import SAGAS, Runtime
from shape.runtime.store import COMMITTED, COMPENSATING
from shape.sim.faults import BURST_LENGTHS, CALL_FAULTS
from shape.simulation import place_order

BURST_OF_FOUR = BURST_LENGTHS.index(4)


def fault(name):
    return CALL_FAULTS.index(name) + 1


def run(realized, runtime_cls=Runtime):
    config = SAGAS["fixed"]
    return place_order("fixed", ExplicitChoices(realized),
                       agent=lambda store, host, approver: runtime_cls(store, host, approver, config))


def fired(result):
    return {v.invariant for v in result.violations}


def sent(result):
    return [e for e in result.world.effects if (e.tool, e.op) == ("email", "send")]


# The email lands, the process dies before it sees the answer, and four rate limits keep the
# replay from running.
EMAIL_LANDED_THEN_LIMITED = {
    "crash:call:email.send#0": 2,
    "fault:email.send#1": fault("rate_limit_burst"),
    "burst:email.send#1": BURST_OF_FOUR,
}
EMAIL_LANDED_THEN_LIMITED_INDEPENDENTLY = {
    "crash:call:email.send#0": 2,
    **{f"fault:email.send#{i}": fault("rate_limit") for i in range(1, 5)},
}
# The same with the process dying inside the durable write that marks the email done.
EMAIL_SENT_THEN_CRASH_BEFORE_MARK = {
    "crash:durable:intents#5": 1,
    "fault:email.send#1": fault("rate_limit_burst"),
    "burst:email.send#1": BURST_OF_FOUR,
}
CHARGE_LANDED_THEN_LIMITED = {
    "crash:call:payments.charge#0": 2,
    "fault:payments.charge#1": fault("rate_limit_burst"),
    "burst:payments.charge#1": BURST_OF_FOUR,
}


class ForgetsInDoubt(Runtime):
    async def _mark_in_doubt(self, saga_id):
        return None


class NoPivotGuard(ForgetsInDoubt):
    def _pivot_may_have_taken_effect(self, saga_id):
        return False


def test_landed_email_with_exhausted_replays_escalates_and_never_compensates():
    for realized in (EMAIL_LANDED_THEN_LIMITED, EMAIL_LANDED_THEN_LIMITED_INDEPENDENTLY,
                     EMAIL_SENT_THEN_CRASH_BEFORE_MARK):
        result = run(realized)
        saga = result.store.saga("saga-17")
        assert result.ok, [v.message for v in result.violations]
        assert saga.state == "escalated" and len(sent(result)) == 1
        assert any(a["kind"] == "escalate" and a["saga"] == "saga-17"
                   for a in result.store.audit_log)
        assert all(c.refunded is False for c in result.world.charges.values())  # still charged


def test_the_same_paths_compensate_after_delivery_without_the_fix():
    for realized in (EMAIL_LANDED_THEN_LIMITED, EMAIL_LANDED_THEN_LIMITED_INDEPENDENTLY,
                     EMAIL_SENT_THEN_CRASH_BEFORE_MARK):
        broken = run(realized, ForgetsInDoubt)
        assert broken.store.saga("saga-17").state == "compensated"
        assert "rollback_restores_state" in fired(broken)


def test_recovery_records_the_interrupted_intent_as_in_doubt_before_replaying():
    result = run({"crash:call:email.send#0": 2})
    assert result.ok and result.store.saga("saga-17").state == COMMITTED
    assert len(sent(result)) == 1  # the replay used the stable key and did not send twice
    assert result.store.intent("saga-17", "email").state == "done"


def test_a_charge_that_landed_before_the_crash_is_refunded_not_forgotten():
    result = run(CHARGE_LANDED_THEN_LIMITED)
    assert result.ok and result.store.saga("saga-17").state == "compensated"
    assert len(result.world.charges) == 1 and all(c.refunded for c in result.world.charges.values())
    broken = run(CHARGE_LANDED_THEN_LIMITED, ForgetsInDoubt)
    assert "rollback_restores_state" in fired(broken)


def test_the_pivot_guard_refuses_to_compensate_after_a_successful_send():
    class CompensatesAfterSend(Runtime):
        async def _settle(self, saga_id, *, state=None, last=None, why=""):
            if state == COMMITTED:
                state = COMPENSATING
            await super()._settle(saga_id, state=state, last=last, why=why)

    result = run({}, CompensatesAfterSend)
    assert result.ok and result.store.saga("saga-17").state == "escalated"
    assert len(sent(result)) == 1
    # without the guard the same mutation compensates a delivered email
    assert "rollback_restores_state" in fired(run({}, type(
        "Both", (CompensatesAfterSend, NoPivotGuard), {})))



# The TLC model found this after the first fix. Four rate limits exhaust the attempts and the
# crash lands just after the intent is written `failed`. The replay then sends the email and
# the process dies again before it sees the result. The intent still reads `failed`, not in
# doubt, so the next replay's four rate limits made the saga compensate a delivered email.
FAILED_INTENT_RETRIED_THEN_CRASHED = {
    "fault:email.send#0": fault("rate_limit_burst"), "burst:email.send#0": BURST_OF_FOUR,
    "crash:durable:intents#5": 2,
    "crash:call:email.send#4": 2,
    "fault:email.send#5": fault("rate_limit_burst"), "burst:email.send#5": BURST_OF_FOUR,
}


def test_a_failed_intent_that_is_called_again_is_in_doubt_after_the_next_crash():
    result = run(FAILED_INTENT_RETRIED_THEN_CRASHED)
    assert result.realized == FAILED_INTENT_RETRIED_THEN_CRASHED  # every deviation was reached
    assert result.ok, [v.message for v in result.violations]
    assert result.store.saga("saga-17").state == "escalated" and len(sent(result)) == 1
