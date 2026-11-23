import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from scenarios import (
    always_raises,
    async_never_awaited,
    async_tool,
    cleanup_probe,
    hangs,
    mail_with_approval,
    mail_without_approval,
    mark_before_effect,
    naive_retry,
    spawned,
    tool,
)

import shape
from shape.boundary import runner
from shape.boundary.faults import FAULTS

ROOT = Path(__file__).resolve().parents[2]


def fault(name: str) -> int:
    return FAULTS.index(name) + 1


def run_explicit(scen, choices, tmp_path):
    return runner.execute(scen, {"explicit": choices}, tmp_path / "w")


def test_crash_after_kills_the_process_without_cleanup(tmp_path):
    out = run_explicit(cleanup_probe, {"fault:t.op#0.0": fault("crash_after")}, tmp_path)
    work = out.dir
    assert out.completed, "the restarted incarnation finished the scenario"
    # incarnation 0 died inside the call: neither its finally block nor its atexit hook ran
    assert not (work / "finally-0").exists()
    assert not (work / "atexit-0").exists()
    # the restart is a new process that ran to the end
    assert (work / "finally-1").exists()
    hows = [e["how"] for e in out.entries if e["kind"] == "incarnation"]
    assert hows == ["crash", "done"]


def test_world_log_survives_the_crash_and_restart_resumes(tmp_path):
    out = run_explicit(cleanup_probe, {"fault:t.op#0.0": fault("crash_after")}, tmp_path)
    effects = [e for e in out.entries if e["kind"] == "effect"]
    # the effect landed in incarnation 0 although the process died before returning
    assert [(e["inc"], e["fault"]) for e in effects] == [(0, "crash_after"), (1, None)]
    log = (out.dir / "world.jsonl").read_text()
    assert log.count('"kind": "effect"') == 2


def test_crash_before_leaves_no_effect(tmp_path):
    out = run_explicit(cleanup_probe, {"fault:t.op#0.0": fault("crash_before")}, tmp_path)
    effects = [e for e in out.entries if e["kind"] == "effect"]
    assert [e["inc"] for e in effects] == [1]


def test_same_seed_gives_the_same_result(tmp_path):
    first = runner.run_seed(naive_retry, 5, tmp_path / "a")
    second = runner.run_seed(naive_retry, 5, tmp_path / "b")
    assert first.realized == second.realized
    assert [v.message for v in first.violations] == [v.message for v in second.violations]


def test_at_most_once_violation_is_found_and_shrunk(tmp_path):
    with pytest.raises(AssertionError) as info:
        shape.check(naive_retry, seeds=200, runs_dir=tmp_path)
    text = str(info.value)
    assert "at_most_once(t.pay)" in text
    assert "NOT MINIMIZED" not in text
    min_file = next((tmp_path / "naive_retry").glob("seed-*.min.json"))
    data = json.loads(min_file.read_text())
    assert len(data["realized"]) == 1
    assert next(iter(data["realized"])).startswith("fault:t.pay#0.0")


def test_replay_of_min_json_reproduces(tmp_path):
    with pytest.raises(AssertionError):
        shape.check(naive_retry, seeds=200, runs_dir=tmp_path)
    min_file = next((tmp_path / "naive_retry").glob("seed-*.min.json"))
    code, text = runner.replay_file(naive_retry, min_file, tmp_path)
    assert code == 1
    assert "at_most_once(t.pay)" in text


def test_replay_reports_when_the_failure_is_gone(tmp_path):
    realized = {"fault:t.pay#0.0": fault("timeout_after_commit")}
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"invariant": "at_most_once(t.pay)", "realized": realized}))

    @shape.scenario(invariants=[shape.at_most_once("t.pay")])
    def careful(run):
        for _ in range(2):
            try:
                return pay_keyed()
            except TimeoutError:
                continue

    code, text = runner.replay_file(careful, path, tmp_path)
    assert code == 0, text
    assert text.startswith("PASS")


@shape.effect("t.pay", scope=lambda **_: "order:1", key=lambda **_: "k1")
def pay_keyed():
    return None


def test_exactly_once_catches_a_lost_effect(tmp_path):
    with pytest.raises(AssertionError) as info:
        shape.check(mark_before_effect, seeds=300, runs_dir=tmp_path)
    assert "exactly_once(t.pay)" in str(info.value)
    assert "lost effect" in str(info.value)


def test_approved_before_catches_an_unapproved_irreversible_effect(tmp_path):
    with pytest.raises(AssertionError) as info:
        shape.check(mail_without_approval, seeds=3, runs_dir=tmp_path)
    assert "approved_before(t.mail)" in str(info.value)
    shape.check(mail_with_approval, seeds=20, runs_dir=tmp_path)


def test_scenario_terminates_catches_a_scenario_that_never_finishes(tmp_path):
    with pytest.raises(AssertionError) as info:
        shape.check(always_raises, seeds=1, runs_dir=tmp_path)
    text = str(info.value)
    assert "scenario_terminates" in text
    assert "max_restarts=2 (3 incarnations)" in text
    assert "cannot recover" in text


def test_a_hung_incarnation_is_killed_and_counts_as_unfinished(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "INCARNATION_TIMEOUT", 0.3)
    out = runner.execute(hangs, {"seed": 0}, tmp_path / "w")
    assert not out.completed
    assert [e["how"] for e in out.entries if e["kind"] == "incarnation"] == ["timeout", "timeout"]


def test_threaded_tool_calls_get_stable_keys(tmp_path):
    @shape.scenario(max_restarts=0)
    def pool(run):
        results = []
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(tool, f"n{i}") for i in range(12)]
        for f in futures:
            try:
                results.append(f.result())
            except TimeoutError:
                results.append("timeout")
        (run.dir / "results.json").write_text(json.dumps(results))

    faulty = {f"fault:t.op#0.{n}": fault("timeout") for n in (1, 4, 7)}
    runs = [run_explicit(pool, faulty, tmp_path / str(i)) for i in range(3)]
    for out in runs:
        assert out.completed
        # exactly the calls numbered 1, 4 and 7 failed, whichever thread made them
        assert out.realized == faulty
        results = json.loads((out.dir / "results.json").read_text())
        assert results.count("timeout") == 3
        assert sum(1 for e in out.entries if e["kind"] == "effect") == 9


def test_a_run_that_cannot_be_replayed_is_reported_not_minimized(tmp_path):
    flag = tmp_path / "flag"

    @shape.scenario(invariants=[shape.at_most_once("t.pay")], max_restarts=0)
    def flaky(run):
        # not controlled by shape: the first execution on this machine double pays, replays do not
        first = not flag.exists()
        flag.write_text("seen")
        pay_twice = first
        for _ in range(2 if pay_twice else 1):
            pay_unkeyed()

    with pytest.raises(AssertionError) as info:
        shape.check(flaky, seeds=1, runs_dir=tmp_path / "runs")
    text = str(info.value)
    assert "NOT MINIMIZED" in text
    assert "did not fail again" in text
    assert "minimized to" not in text


@shape.effect("t.pay", scope=lambda **_: "order:1")
def pay_unkeyed():
    return None


def test_spawn_fallback_runs_incarnations_in_fresh_interpreters(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAPE_START_METHOD", "spawn")
    spawned.file = str(Path(__file__).with_name("scenarios.py"))
    out = run_explicit(spawned, {"fault:t.op#0.0": fault("crash_after")}, tmp_path)
    assert out.ok, out.violations
    assert [e["how"] for e in out.entries if e["kind"] == "incarnation"] == ["crash", "done"]


def test_last_incarnation_is_fault_free_so_a_recovering_agent_terminates(tmp_path):
    explicit = {f"fault:t.mail#{i}.0": FAULTS.index("crash_before") + 1 for i in range(4)}
    out = runner.execute(mail_with_approval, {"explicit": explicit}, tmp_path / "w")
    assert out.completed
    assert not [v for v in out.violations if v.invariant == "scenario_terminates"]



def test_parallel_sweep_prints_the_same_report_as_a_serial_one(tmp_path):
    import re
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[2]

    def report(jobs: int) -> str:
        out = subprocess.run(
            [sys.executable, "-m", "shape.cli", "test", "examples/plain_python/checks.py",
             "--seeds", "200", "--jobs", str(jobs), "--runs-dir", str(tmp_path / f"j{jobs}")],
            check=False, cwd=root, capture_output=True, text=True,
            env={"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin"})
        return re.sub(r"wall=\S+\s+\d+ seeds/s|j\d+", "", out.stdout)

    serial = report(1)
    assert "FAIL" in serial
    assert report(4) == serial


def test_async_crash_after_fires_after_the_awaited_body_ran(tmp_path):
    out = run_explicit(async_tool, {"fault:t.apay#0.0": fault("crash_after")}, tmp_path)
    effects = [e for e in out.entries if e["kind"] == "effect"]
    assert [(e["inc"], e["fault"]) for e in effects] == [(0, "crash_after")]
    assert effects[0]["call"].startswith("apay(order='1'")
    # the body really ran before the process died (a wrapper that faults at coroutine creation
    # kills the process before the body, so the effect would be logged with no body behind it)
    assert (out.dir / "apay-body").read_text() == "body ran\n"
    assert not out.violated("exactly_once(t.apay)")


def test_async_crash_before_leaves_no_effect_and_no_body_run(tmp_path):
    out = run_explicit(async_tool, {"fault:t.apay#0.0": fault("crash_before")}, tmp_path)
    assert [e for e in out.entries if e["kind"] == "effect"] == []
    assert not (out.dir / "apay-body").exists()


def test_async_after_commit_fault_raises_from_the_await_with_the_body_run(tmp_path):
    out = run_explicit(async_tool, {"fault:t.apay#0.0": fault("timeout_after_commit")}, tmp_path)
    effects = [e for e in out.entries if e["kind"] == "effect"]
    assert [(e["inc"], e["fault"]) for e in effects] == [(0, "timeout_after_commit")]
    assert (out.dir / "apay-body").read_text() == "body ran\n"
    assert any(e["kind"] == "error" and "TimeoutError" in e["text"] for e in out.entries)


def test_async_before_commit_fault_raises_from_the_await_and_the_body_does_not_run(tmp_path):
    out = run_explicit(async_tool, {"fault:t.apay#0.0": fault("timeout")}, tmp_path)
    assert [e for e in out.entries if e["kind"] == "effect"] == []
    assert not (out.dir / "apay-body").exists()
    assert any(e["kind"] == "error" and "TimeoutError" in e["text"] for e in out.entries)
    # incarnation 0 died with the timeout and the restart does not redo it: the effect is lost
    assert out.violated("exactly_once(t.apay)")


def test_a_coroutine_that_is_never_awaited_draws_no_fault_and_records_nothing(tmp_path):
    out = run_explicit(async_never_awaited, {}, tmp_path)
    assert out.completed
    assert [e for e in out.entries if e["kind"] in ("effect", "fault")] == []


def test_async_effect_is_the_plain_coroutine_function_outside_a_run():
    import asyncio
    import inspect

    from scenarios import apay
    assert inspect.iscoroutinefunction(apay)
    assert asyncio.run(apay("9")) == "9"


def test_choose_returns_the_nominal_option_outside_a_run():
    assert shape.choose("x", ["nominal", "other"]) == "nominal"


def test_a_bug_reachable_only_through_a_model_choice_is_found_and_shrunk_to_it(tmp_path):
    from scenarios import model_may_repeat_a_refund
    with pytest.raises(AssertionError) as info:
        shape.check(model_may_repeat_a_refund, seeds=200, runs_dir=tmp_path)
    text = str(info.value)
    assert "at_most_once(t.refund)" in text
    assert "model chose 'refund_again' at 'after_refund' (nominal: 'reply_to_customer')" in text
    assert "1 of 1 faults kept" in text  # the choice alone, every injected fault shrunk away


def test_model_choices_replay_exactly(tmp_path):
    from scenarios import model_may_repeat_a_refund
    explicit = {"model:after_refund#0.0": 1}
    first = runner.execute(model_may_repeat_a_refund, {"explicit": explicit}, tmp_path / "a")
    again = runner.execute(model_may_repeat_a_refund, {"explicit": explicit}, tmp_path / "b")
    assert [v.invariant for v in first.violations] == ["at_most_once(t.refund)"]
    assert first.realized == again.realized == explicit


def test_harmless_choices_do_not_fail_a_correct_scenario(tmp_path):
    from scenarios import model_never_repeats
    shape.check(model_never_repeats, seeds=100, runs_dir=tmp_path)


def test_choose_rejects_mismatched_weights(tmp_path):
    @shape.scenario(max_restarts=0)
    def bad(run):
        shape.choose("x", ["a", "b"], weights=[1.0])
    out = runner.execute(bad, {"seed": 0}, tmp_path / "w")
    assert not out.completed
