"""The adapters forward every `@shape.effect` option, and traces name the planned action."""

from __future__ import annotations

import inspect

import pytest

import shape
from shape.adapters._plan import (
    Call,
    Effect,
    HistoryUser,
    PlanRunner,
    make_effect_decorator,
    normalize_effect,
)
from shape.boundary import api, runner


def test_effect_has_a_field_for_every_shape_effect_option():
    options = set(inspect.signature(api.effect).parameters) - {"op"}
    assert options == {f for f in Effect.__dataclass_fields__} - {"op"}


def test_normalize_effect_keeps_the_new_options_and_still_rejects_unknown_keys():
    landed = lambda reply: reply > 0
    key_from_result = lambda reply: f"k:{reply}"
    cfg = {
        "op": "x.put",
        "idempotent": True,
        "result": len,
        "hide": ["secret"],
        "landed": landed,
        "key_from_result": key_from_result,
    }
    e = normalize_effect("t", cfg)
    assert (e.idempotent, e.result, tuple(e.hide), e.landed, e.key_from_result) == (
        True,
        len,
        ("secret",),
        landed,
        key_from_result,
    )
    with pytest.raises(ValueError, match="unknown effect option"):
        normalize_effect("t", {"op": "x", "idempotency": True})


@shape.effect("t.plain")
def _unused() -> None:
    pass


_put = make_effect_decorator(Effect(
    op="t.adapter_put", scope=lambda order, **_: f"order:{order}", idempotent=True,
    result=lambda reply: {"n": reply}, hide=("secret",)))(
    lambda order, value, secret="s": 7)


@shape.scenario(invariants=[shape.at_most_once("t.adapter_put")], max_restarts=0)
def puts_twice(run: shape.Run) -> None:
    _put("1", "a", secret="hunter2")
    _put("1", "a", secret="hunter2")


def test_the_decorator_built_from_an_adapter_effect_honours_every_option(tmp_path):
    out = runner.execute(puts_twice, {"explicit": {}}, tmp_path / "w")
    assert out.ok  # idempotent=True: the repeated call is one effect
    effect = next(e for e in out.entries if e["kind"] == "effect")
    assert effect["result"] == {"n": 7}  # result=
    assert "secret" not in effect["call"] and "secret" not in effect["args"]  # hide=


def test_a_perturbed_plan_names_the_planned_call_on_the_nominal_side(tmp_path):
    @shape.scenario(invariants=[], max_restarts=0)
    def planned(run: shape.Run) -> None:
        plan = PlanRunner([Call("issue_refund", {"order_id": "17"})])
        plan.decide([HistoryUser()])

    out = runner.execute(planned, {"explicit": {"model:call 1: issue_refund#0.0": 1}},
                         tmp_path / "w")
    trace, _ = runner.story(out.entries)
    assert ("model chose 'repeat' at 'call 1: issue_refund' "
            "(nominal: issue_refund(order_id='17'))") in "\n".join(trace)
