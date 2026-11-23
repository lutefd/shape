"""Replay the TLC findings against the in-repo simulator."""

import argparse

from shape.runtime.store import COMMITTED, ESCALATED
from shape.sim.faults import BURST_LENGTHS, CALL_FAULTS
from shape.simulation import ORDER, replay

REQUEST = f"saga-{ORDER['order']}:email"
EMAIL_ARGS = {key: ORDER[key] for key in ("order", "to")}
BURST_OF_FOUR = BURST_LENGTHS.index(4)


def fault(name):
    return CALL_FAULTS.index(name) + 1


def approval_contract():
    for answer, expected in ((0, True), (1, False), (2, True), (3, False)):
        result = replay("fixed", {f"human:{REQUEST}": answer})
        sent = [e for e in result.world.effects if (e.tool, e.op) == ("email", "send")]
        approved = False
        authorized_call = False
        for row in result.store.audit_log:
            if (row["kind"] == "gate_decision" and row["request"] == REQUEST
                    and row["approved"] is True):
                approved = True
            if (row["kind"] == "call" and (row["tool"], row["op"]) == ("email", "send")
                    and row["args"] == EMAIL_ARGS):
                authorized_call |= approved
        for effect in sent:
            assert authorized_call and effect.args == EMAIL_ARGS, (
                "email.send happened without durable approval for its exact action"
            )
        assert len(sent) == int(expected), f"unexpected number of sends for human outcome {answer}"
    print("DST approval: approved and delayed send with a durable decision. Rejected and silent stop.")


def report_findings():
    for saga, expected in (("toy", 2), ("fixed", 1)):
        result = replay(saga, {"fault:payments.charge#0": fault("timeout_after_commit")})
        keys = [e.key for e in result.world.effects if (e.tool, e.op) == ("payments", "charge")]
        assert len(keys) == expected, keys
        print(f"DST {saga}: charge keys {keys}")

    first_write = replay("fixed", {"crash:durable:sagas#0": 1})
    assert not first_write.violations and first_write.world.submitted
    assert first_write.store.saga("saga-17").state == COMMITTED
    assert any("resubmitting" in e.text for e in first_write.trace.events if e.kind == "client")
    print("DST fixed submission: pre-write crash lost no order. The client resubmitted until acked.")

    later_calls = {f"fault:email.send#{i}": fault("rate_limit") for i in range(1, 5)}
    lost_response = replay("fixed", {"crash:call:email.send#0": 2, **later_calls})
    assert_escalated_after_delivery(lost_response)
    print("DST fixed compensation: email landed, recovery rate-limited four times, then escalated.")

    stale_failure = replay("fixed", {
        "fault:email.send#0": fault("rate_limit_burst"), "burst:email.send#0": BURST_OF_FOUR,
        "crash:durable:intents#5": 2, "crash:call:email.send#4": 2,
        "fault:email.send#5": fault("rate_limit_burst"), "burst:email.send#5": BURST_OF_FOUR,
    })
    assert_escalated_after_delivery(stale_failure)
    print("DST fixed retry: a failed intent called again was in doubt after the second crash.")


def assert_escalated_after_delivery(result):
    assert not result.violations, [v.message for v in result.violations]
    assert result.store.saga("saga-17").state == ESCALATED
    assert len([e for e in result.world.effects if (e.tool, e.op) == ("email", "send")]) == 1
    assert any(row["kind"] == "escalate" for row in result.store.audit_log)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", nargs="?", choices=("all", "approval"), default="all")
    args = parser.parse_args()
    approval_contract()
    if args.check == "all":
        report_findings()
