"""Outcome semantics and the invariants over the world log. Each invariant has a test that makes
it fire and a test where a correct scenario passes."""

import pytest
import scenarios as sc

import shape
from shape.boundary import runner
from shape.boundary.faults import FAULTS


def fault(name: str) -> int:
    return FAULTS.index(name) + 1


def run(scen, choices, tmp_path):
    return runner.execute(scen, {"explicit": choices}, tmp_path / "w")


def violated(out) -> list[str]:
    return [v.invariant for v in out.violations]


def test_idempotent_effect_repeated_with_the_same_arguments_is_one_effect(tmp_path):
    assert run(sc.put_same_value_twice, {}, tmp_path).ok


def test_idempotent_effect_with_different_arguments_is_two_effects(tmp_path):
    assert violated(run(sc.put_two_values, {}, tmp_path)) == ["at_most_once(t.put)"]


def test_an_effect_without_idempotent_counts_every_repeat(tmp_path):
    out = run(sc.naive_retry, {"fault:t.pay#0.0": fault("timeout_after_commit")}, tmp_path)
    assert violated(out) == ["at_most_once(t.pay)"]


def test_result_is_logged_for_invariants_to_read(tmp_path):
    out = run(sc.model_may_overcharge, {"model:amount#0.0": 1}, tmp_path)
    effect = next(e for e in out.entries if e["kind"] == "effect")
    assert effect["result"] == {"status": "ok"}  # what result= kept, not the whole reply
    assert violated(out) == ["charges_stay_small"]
    assert "charged 500, over the 100 cap" in out.violations[0].message
    assert run(sc.model_may_overcharge, {}, tmp_path).ok


def test_requires_catches_a_notification_for_a_charge_that_never_landed(tmp_path):
    out = run(sc.notify_even_if_charge_failed, {"fault:t.charge#0.0": fault("timeout")}, tmp_path)
    assert violated(out) == ["requires(t.notify, before=t.charge)"]
    assert "no earlier t.charge that landed for scope 'order:1'" in out.violations[0].message


def test_requires_accepts_a_charge_that_landed_even_if_the_caller_saw_a_timeout(tmp_path):
    out = run(sc.notify_even_if_charge_failed,
              {"fault:t.charge#0.0": fault("timeout_after_commit")}, tmp_path)
    assert out.ok


def test_requires_passes_for_a_correct_scenario_under_every_fault(tmp_path):
    shape.check(sc.notify_only_after_charge, seeds=200, runs_dir=tmp_path)


def test_requires_same_scope_rejects_a_charge_for_another_order(tmp_path):
    assert violated(run(sc.notify_after_charge_of_another_order_same_scope, {}, tmp_path))
    assert run(sc.notify_after_charge_of_another_order, {}, tmp_path).ok


def test_within_scopes_catches_the_model_picking_the_wrong_target(tmp_path):
    out = run(sc.model_may_refund_the_wrong_order, {"model:which_order#0.0": 1}, tmp_path)
    assert violated(out) == ["within_scopes(t.refund)"]
    assert "acted on 'order:2', outside the allowed scopes ('order:1')" in out.violations[0].message
    assert run(sc.model_refunds_the_right_order, {}, tmp_path).ok


def test_within_scopes_found_by_a_seed_sweep_and_shrunk_to_the_choice(tmp_path):
    with pytest.raises(AssertionError) as info:
        shape.check(sc.model_may_refund_the_wrong_order, seeds=200, runs_dir=tmp_path)
    assert "model chose '2' at 'which_order' (nominal: '1')" in str(info.value)


def test_within_scopes_allowed_can_be_a_function_of_the_run(tmp_path):
    out = run(sc.refund_outside_the_allowed_file, {}, tmp_path)
    assert "acted on 'order:3'" in out.violations[0].message


def test_max_calls_counts_failed_attempts_so_a_retry_storm_is_caught(tmp_path):
    storm = {f"fault:t.charge#0.{n}": fault("timeout") for n in range(5)}
    out = run(sc.unbounded_retries, storm, tmp_path)
    assert violated(out) == ["max_calls(t.charge, 3)"]
    assert "6 times for scope 'order:1', failed calls included" in out.violations[0].message
    # five of the six calls never landed, so an effect-counting invariant could not see this
    assert len([e for e in out.entries if e["kind"] == "effect"]) == 1


def test_max_calls_passes_for_a_bounded_retry_loop(tmp_path):
    storm = {f"fault:t.charge#0.{n}": fault("timeout") for n in range(5)}
    out = run(sc.bounded_retries, storm, tmp_path)
    assert len(out.entries) and out.ok


def test_sum_le_catches_an_over_refund_and_passes_within_the_limit(tmp_path):
    out = run(sc.model_may_over_refund, {"model:after_refund#0.0": 1}, tmp_path)
    assert "t.pay_out amount added up to 150 for scope 'order:1' (limit 100)" in (
        out.violations[0].message)
    assert run(sc.refunds_within_the_limit, {}, tmp_path).ok  # 60 + 40, and order 2 separately


def test_claims_true_catches_telling_the_user_a_refund_that_does_not_exist(tmp_path):
    out = run(sc.claims_refund_even_if_it_failed, {"fault:t.refund#0.0": fault("timeout")}, tmp_path)
    assert violated(out) == ["claims_true"]
    assert "told the user t.refund happened for scope 'order:1'" in out.violations[0].message
    assert run(sc.claims_refund_even_if_it_failed, {}, tmp_path).ok


def test_claims_true_passes_for_an_agent_that_claims_only_what_landed(tmp_path):
    shape.check(sc.claims_only_what_landed, seeds=200, runs_dir=tmp_path)


def test_a_custom_invariant_that_raises_is_a_finding_not_a_crash(tmp_path):
    out = run(sc.scenario_with_a_broken_invariant, {}, tmp_path)
    assert violated(out) == ["broken_invariant"]
    assert "the invariant itself raised KeyError: 'nope'" in out.violations[0].message


def test_exactly_once_does_not_reuse_the_at_most_once_message(tmp_path):
    out = run(sc.naive_retry_exactly_once, {"fault:t.pay#0.0": fault("timeout_after_commit")},
              tmp_path)
    message = out.violations[0].message
    assert "exactly once" in message
    assert "at most 1 allowed" not in message


def test_approved_before_match_maps_the_raw_arguments_to_what_the_human_approved(tmp_path):
    assert run(sc.raw_body_with_match, {}, tmp_path).ok
    assert violated(run(sc.raw_body_without_match, {}, tmp_path)) == ["approved_before(t.raw_send)"]
