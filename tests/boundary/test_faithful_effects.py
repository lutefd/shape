"""Tests for faithful effect semantics: landed=, key_from_result=, harness errors,
NOTHING TESTED diagnostics, coarse distinct-cause grouping, nominal-path labeling,
disk cleanup, and exactly_once(op, failure_claim_ok=True)."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
import scenarios as sc

import shape
from shape.boundary import runner
from shape.boundary.faults import FAULTS


def fault(name: str) -> int:
    return FAULTS.index(name) + 1


def run_explicit(scen, choices, tmp_path):
    return runner.execute(scen, {"explicit": choices}, tmp_path / "w")


# -------------------------------------------------------------------------------------- 1. landed=


def test_landed_false_records_attempt_not_effect_and_renders_in_trace(tmp_path):
    @shape.effect(
        "orders.cancel",
        scope=lambda order_id, **_: f"order:{order_id}",
        landed=lambda res: not (isinstance(res, str) and res.startswith("refused:")),
    )
    def cancel(order_id: str, owner: str) -> str:
        if owner != "alice":
            return "refused: not the owner"
        return f"cancelled:{order_id}"

    @shape.scenario(
        invariants=[
            shape.at_most_once("orders.cancel"),
            shape.requires("orders.notify", before="orders.cancel"),
        ],
        max_restarts=0,
    )
    def refused_then_succeeded(run: shape.Run) -> None:
        assert cancel("17", owner="bob") == "refused: not the owner"
        assert cancel("17", owner="bob") == "refused: not the owner"
        assert cancel("17", owner="alice") == "cancelled:17"

    out = run_explicit(refused_then_succeeded, {}, tmp_path / "a")
    assert out.ok, out.violations
    effects = [e for e in out.entries if e["kind"] == "effect"]
    attempts = [e for e in out.entries if e["kind"] == "attempt"]
    no_effects = [e for e in out.entries if e["kind"] == "no_effect"]
    assert len(effects) == 1
    assert len(attempts) == 3
    assert len(no_effects) == 2
    trace, _ = runner.story(out.entries)
    assert sum("returned without effect" in line for line in trace) == 2
    assert sum("effect landed" in line for line in trace) == 1

    # An after-commit fault drawn on a refused call must NOT log a bogus effect
    out_fault = run_explicit(
        refused_then_succeeded,
        {"fault:orders.cancel#0.0": fault("timeout_after_commit")},
        tmp_path / "b",
    )
    assert out_fault.ok, out_fault.violations
    assert len([e for e in out_fault.entries if e["kind"] == "effect"]) == 1


def test_landed_works_for_async_effects_and_error_dicts(tmp_path):
    @shape.effect(
        "tickets.create",
        scope=lambda ticket_id, **_: f"ticket:{ticket_id}",
        landed=lambda res: bool(res.get("ok")),
    )
    async def create_ticket(ticket_id: str, valid: bool) -> dict:
        await asyncio.sleep(0)
        return {"ok": True, "id": ticket_id} if valid else {"ok": False, "error": "invalid"}

    @shape.scenario(invariants=[shape.at_most_once("tickets.create")], max_restarts=0)
    def async_refusal(run: shape.Run) -> None:
        asyncio.run(create_ticket("1", False))
        asyncio.run(create_ticket("1", False))
        asyncio.run(create_ticket("1", True))

    out = run_explicit(async_refusal, {}, tmp_path)
    assert out.ok, out.violations
    assert len([e for e in out.entries if e["kind"] == "effect"]) == 1
    trace, _ = runner.story(out.entries)
    assert any("returned without effect" in line for line in trace)


# ------------------------------------------------------------------------------- A. key_from_result


def test_key_from_result_dedupes_repeat_calls_yielding_same_id(tmp_path):
    @shape.effect(
        "cases.open",
        scope=lambda user_id, **_: f"user:{user_id}",
        key_from_result=lambda res: res.get("case_id"),
    )
    def open_case(user_id: str, dedupe: bool = True) -> dict:
        if dedupe:
            return {"case_id": "CMP-5170"}
        nonlocal counter
        counter += 1
        return {"case_id": f"CMP-{counter}"}

    counter = 5170

    @shape.scenario(invariants=[shape.at_most_once("cases.open")], max_restarts=0)
    def deduped_via_state(run: shape.Run) -> None:
        open_case("u1", dedupe=True)
        open_case("u1", dedupe=True)

    out_ok = run_explicit(deduped_via_state, {}, tmp_path / "ok")
    assert out_ok.ok, out_ok.violations
    raw_effects = [e for e in out_ok.entries if e["kind"] == "effect"]
    assert [e["key"] for e in raw_effects] == ["CMP-5170", "CMP-5170"]

    @shape.scenario(invariants=[shape.at_most_once("cases.open")], max_restarts=0)
    def creates_two_cases(run: shape.Run) -> None:
        open_case("u1", dedupe=False)
        open_case("u1", dedupe=False)

    out_bad = run_explicit(creates_two_cases, {}, tmp_path / "bad")
    assert out_bad.violated("at_most_once(cases.open)")


# ------------------------------------------------------------------ 3. Harness errors & diagnostics


def test_hook_errors_fail_loudly_even_when_caught_by_except_exception(tmp_path):
    for hook_name, kwargs in [
        ("scope", {"scope": lambda wrong_param: wrong_param}),
        ("key", {"scope": lambda x, **_: x, "key": lambda wrong_param: wrong_param}),
        ("landed", {"scope": lambda x, **_: x, "landed": lambda res: 1 / 0}),
        ("key_from_result", {"scope": lambda x, **_: x, "key_from_result": lambda res: res["nope"]}),
        ("result", {"scope": lambda x, **_: x, "result": lambda res: res["nope"]}),
    ]:
        @shape.effect("t.hook_err", **kwargs)
        def op(x: str) -> dict:
            return {"ok": True}

        @shape.scenario(invariants=[shape.at_most_once("t.hook_err")], max_restarts=3)
        def swallows_exception(run: shape.Run) -> None:
            with contextlib.suppress(Exception):
                op("1")

        out = run_explicit(swallows_exception, {}, tmp_path / hook_name)
        assert not out.ok
        assert out.violations[0].invariant == "harness_error"
        msg = out.violations[0].message
        assert f"your {hook_name} for t.hook_err raised " in msg
        assert "test_faithful_effects.py:" in msg
        # Must stop on incarnation 0 instead of retrying max_restarts times
        assert len([e for e in out.entries if e["kind"] == "incarnation"]) == 1


def test_nothing_tested_explains_no_attempts_vs_refused_vs_raised(tmp_path):
    @shape.effect("t.refused", scope=lambda **_: "s", landed=lambda _r: False)
    def always_refused() -> str:
        return "not allowed"

    @shape.scenario(invariants=[shape.at_most_once("t.refused")], max_restarts=1)
    def all_refused(run: shape.Run) -> None:
        with contextlib.suppress(Exception):
            always_refused()

    with pytest.raises(AssertionError) as info_refused:
        shape.check(all_refused, seeds=5, runs_dir=tmp_path / "refused")
    text_refused = str(info_refused.value)
    assert "NOTHING TESTED" in text_refused
    assert "0 landed" in text_refused
    assert "returned without effect" in text_refused

    @shape.effect("t.broken", scope=lambda **_: "s")
    def always_raises_in_tool() -> None:
        raise ValueError("tool rejected")

    @shape.scenario(invariants=[shape.at_most_once("t.broken")], max_restarts=0)
    def all_raised(run: shape.Run) -> None:
        with contextlib.suppress(Exception):
            always_raises_in_tool()

    with pytest.raises(AssertionError) as info_raised:
        shape.check(all_raised, seeds=5, runs_dir=tmp_path / "raised")
    text_raised = str(info_raised.value)
    assert "NOTHING TESTED" in text_raised
    assert "0 landed" in text_raised
    assert "raised before completing" in text_raised


# -------------------------------------------- 5, C, D. Coarse cause grouping & nominal-path label


def test_after_commit_faults_group_into_one_cause_with_exact_kinds_listed(tmp_path):
    swept = runner.run_sweep(sc.naive_retry, range(300), tmp_path)
    report = runner.investigate_all(sc.naive_retry, swept.failing, tmp_path, "x")
    assert len(report.causes) == 1
    text = report.render()
    assert "1 distinct cause across" in text
    assert "needs: after-commit fault" in text
    assert "the caller did not see the commit" in text
    for kind_name in ("timeout_after_commit", "connection_reset_after_commit", "crash_after"):
        assert kind_name in text


def test_before_commit_faults_group_into_one_cause_separate_from_crash_before(tmp_path):
    @shape.effect("t.fragile", scope=lambda **_: "s")
    def fragile() -> None:
        pass

    @shape.scenario(invariants=[shape.claims_true()], max_restarts=1)
    def claims_after_any_failure(run: shape.Run) -> None:
        if run.restarts == 0:
            try:
                fragile()
            except Exception:  # noqa: BLE001
                shape.claim("s", "t.fragile")
                return
            shape.claim("s", "t.fragile")
        else:
            # Crashed before call on inc 0 -> restarted inc 1 claims without calling!
            shape.claim("s", "t.fragile")

    swept = runner.run_sweep(claims_after_any_failure, range(300), tmp_path)
    report = runner.investigate_all(claims_after_any_failure, swept.failing, tmp_path, "x")
    assert len(report.causes) == 2
    text = report.render()
    assert "the call failed cleanly" in text
    assert "died before the call" in text
    assert "timeout" in text and "server_error" in text and "crash_before" in text


def test_nominal_path_cause_is_labeled_and_printed_last(tmp_path):
    @shape.effect("t.always_bad", scope=lambda **_: "s")
    def always_bad() -> None:
        pass

    @shape.effect("t.fault_bad", scope=lambda **_: "s")
    def fault_bad() -> None:
        pass

    @shape.scenario(
        invariants=[shape.at_most_once("t.fault_bad"), shape.approved_before("t.always_bad")],
        max_restarts=0,
    )
    def both_bugs(run: shape.Run) -> None:
        # Fault-triggered bug checked first so seeds with an after-commit fault hit at_most_once,
        # while nominal seeds hit approved_before with 0 faults and 0 model choices.
        for _ in range(2):
            try:
                fault_bad()
                break
            except (TimeoutError, ConnectionResetError):
                continue
            except Exception:  # noqa: BLE001 - clean before-commit errors just stop retrying
                break
        with contextlib.suppress(Exception):
            always_bad()

    swept = runner.run_sweep(both_bugs, range(100), tmp_path)
    report = runner.investigate_all(both_bugs, swept.failing, tmp_path, "x")
    assert len(report.causes) >= 2
    # Nominal-path cause prints LAST even when it has more seeds than fault causes
    assert not report.causes[0].is_nominal
    assert report.causes[-1].is_nominal
    assert len(report.causes[-1].seeds) > len(report.causes[0].seeds)
    text = report.render()
    fault_pos = text.index("needs: after-commit fault")
    nominal_pos = text.index(
        "nominal path (0 faults, 0 model choices): a plain unit test catches this"
    )
    assert fault_pos < nominal_pos


def test_claims_true_groups_distinct_ops_into_distinct_causes(tmp_path):
    @shape.effect("t.op_a", scope=lambda **_: "s")
    def op_a() -> None:
        pass

    @shape.effect("t.op_b", scope=lambda **_: "s")
    def op_b() -> None:
        pass

    @shape.scenario(invariants=[shape.claims_true()], max_restarts=0)
    def lies_about_two_ops(run: shape.Run) -> None:
        with contextlib.suppress(Exception):
            op_a()
        shape.claim("s", "t.op_a")
        with contextlib.suppress(Exception):
            op_b()
        shape.claim("s", "t.op_b")

    swept = runner.run_sweep(lies_about_two_ops, range(200), tmp_path, jobs=4)
    report = runner.investigate_all(
        lies_about_two_ops, swept.failing, tmp_path, "x", failing_ops=swept.failing_ops
    )
    claim_ops = {c.failure.op for c in report.causes if c.failure.invariant == "claims_true"}
    assert claim_ops == {"t.op_a", "t.op_b"}


# --------------------------------------------------------------------------------- 6. Disk cleanup


def test_passing_sweep_deletes_seed_dirs_and_stale_failures(tmp_path):
    runs = tmp_path / "runs"
    # Pre-populate a stale seed dir as if an earlier run failed
    stale = runs / sc.model_never_repeats.name / "seed-0"
    stale.mkdir(parents=True)
    (stale / "world.jsonl").write_text("{}\n")

    shape.check(sc.model_never_repeats, seeds=15, runs_dir=runs)
    # Every passing seed's dir (including stale seed-0) and the empty scenario dir are gone
    assert not (runs / sc.model_never_repeats.name).exists()


def test_failing_sweep_keeps_only_failing_seed_dirs_not_passing_ones(tmp_path):
    runs = tmp_path / "runs"
    swept = runner.run_sweep(sc.naive_retry, range(40), runs)
    failing_seeds = {s for s, _, _ in swept.failing}
    assert 0 < len(failing_seeds) < 40
    kept = {p.name for p in (runs / sc.naive_retry.name).iterdir()}
    assert kept == {f"seed-{s}" for s in failing_seeds}


# --------------------------------------------------- E. exactly_once(op, failure_claim_ok=True)


def test_exactly_once_failure_claim_ok_accepts_honest_failure_and_rejects_lies_or_silence(tmp_path):
    @shape.effect("t.transfer", scope=lambda order, **_: f"order:{order}")
    def transfer(order: str) -> None:
        pass

    @shape.scenario(
        invariants=[
            shape.exactly_once("t.transfer", failure_claim_ok=True),
            shape.claims_true(),
        ],
        max_restarts=0,
    )
    def honest_failure(run: shape.Run) -> None:
        try:
            transfer("1")
        except Exception:  # noqa: BLE001
            shape.claim("order:1", "t.transfer", ok=False)
            return
        shape.claim("order:1", "t.transfer", ok=True)

    # Before-commit timeout + honest failure claim -> PASS
    out_honest = run_explicit(
        honest_failure, {"fault:t.transfer#0.0": fault("timeout")}, tmp_path / "honest"
    )
    assert out_honest.ok, out_honest.violations
    trace, _ = runner.story(out_honest.entries)
    assert any("agent told the user t.transfer did not happen for 'order:1'" in t for t in trace)

    # After-commit timeout + false failure claim ("didn't happen" when it landed!) -> claims_true FAILS
    out_false_neg = run_explicit(
        honest_failure,
        {"fault:t.transfer#0.0": fault("timeout_after_commit")},
        tmp_path / "false_neg",
    )
    assert out_false_neg.violated("claims_true")

    # Lost effect + silence (no claim at all) -> exactly_once FAILS
    @shape.scenario(
        invariants=[shape.exactly_once("t.transfer", failure_claim_ok=True)],
        max_restarts=0,
    )
    def silent_failure(run: shape.Run) -> None:
        with contextlib.suppress(Exception):
            transfer("1")

    out_silent = run_explicit(
        silent_failure, {"fault:t.transfer#0.0": fault("timeout")}, tmp_path / "silent"
    )
    assert out_silent.violated("exactly_once(t.transfer)")

    # Lost effect + claimed success -> exactly_once FAILS
    @shape.scenario(
        invariants=[shape.exactly_once("t.transfer", failure_claim_ok=True)],
        max_restarts=0,
    )
    def false_success(run: shape.Run) -> None:
        with contextlib.suppress(Exception):
            transfer("1")
        shape.claim("order:1", "t.transfer", ok=True)

    out_lie = run_explicit(
        false_success, {"fault:t.transfer#0.0": fault("timeout")}, tmp_path / "lie"
    )
    assert out_lie.violated("exactly_once(t.transfer)")


def test_public_check_jobs_and_replay_api(tmp_path):
    shape.check(sc.model_never_repeats, seeds=20, runs_dir=tmp_path / "p", jobs=2)

    with pytest.raises(AssertionError):
        shape.check(sc.naive_retry, seeds=100, runs_dir=tmp_path / "f", jobs=2)
    min_file = next((tmp_path / "f" / "naive_retry").glob("seed-*.min.json"))

    # Replaying against the buggy scenario raises AssertionError
    with pytest.raises(AssertionError, match=r"at_most_once\(t\.pay\)"):
        shape.replay(sc.naive_retry, min_file, runs_dir=tmp_path / "rep_bad")

    # Replaying against a fixed scenario passes
    @shape.effect("t.pay", scope=lambda order, **_: f"order:{order}", key=lambda order, **_: order)
    def pay_keyed(order: str) -> None:
        pass

    @shape.scenario(invariants=[shape.at_most_once("t.pay")], max_restarts=3)
    def fixed_retry(run: shape.Run) -> None:
        for _ in range(3):
            try:
                return pay_keyed("1")
            except (TimeoutError, ConnectionResetError):
                continue

    shape.replay(fixed_retry, min_file, runs_dir=tmp_path / "rep_ok")


def test_scheduling_choices_group_as_interleaving_coarse_class():
    from shape.boundary.failure import Cause, Failure, coarse_kind

    assert coarse_kind("sched:actors#0.1", 1) == "interleaving"
    assert coarse_kind("sched:tasks#0.0", 2) == "interleaving"
    f = Failure(
        scenario="race",
        target="x",
        seed=1,
        invariant="at_most_once(t.claim)",
        message="happened twice",
        trace=[],
        steps=2,
        deviations=1,
        original_deviations=1,
        replays=2,
        replay_file=None,
        signature=("at_most_once(t.claim)", "interleaving"),
        op="t.claim",
        coarse_deviations=("interleaving",),
        exact_kinds=("sched:actors#0.1=1",),
    )
    cause = Cause(f, seeds=[1, 2])
    assert not cause.is_nominal
    assert cause.trigger_label() == "needs: interleaving"
