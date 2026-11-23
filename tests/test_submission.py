"""The client submits until it sees an ack, and the first durable write precedes the ack."""

from shape.runtime.store import COMMITTED
from shape.simulation import replay


def kinds(result):
    return [e.text for e in result.trace.events if e.kind == "client"]


def test_crash_before_the_first_write_is_resubmitted_and_the_order_completes():
    result = replay("fixed", {"crash:durable:sagas#0": 1})
    assert result.ok and result.world.submitted == [result.store.saga("saga-17").order]
    assert result.store.saga("saga-17").state == COMMITTED
    assert any("resubmitting" in t for t in kinds(result))
    assert len(result.world.charges) == 1


def test_crash_after_the_first_write_is_resubmitted_without_a_second_saga():
    result = replay("fixed", {"crash:durable:sagas#0": 2})
    assert result.ok and list(result.store.sagas) == ["saga-17"]
    assert result.store.saga("saga-17").state == COMMITTED
    assert len(result.world.charges) == 1 and len(result.world.outbox) == 1


def test_no_resubmission_once_the_ack_arrived():
    result = replay("fixed", {"crash:call:payments.charge#0": 1})
    assert result.ok and not any("resubmitting" in t for t in kinds(result))
    assert any("acknowledged" in t for t in kinds(result))
