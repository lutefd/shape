"""Traces a stranger can read: hidden framework arguments, root-cause exceptions, distinct
causes, coverage, flaky seeds, and clean CLI failures."""

import subprocess
import sys
from pathlib import Path

import pytest
import scenarios as sc

import shape
from shape import cli
from shape.boundary import rootcause, runner
from shape.boundary.faults import FAULTS

ROOT = Path(__file__).resolve().parents[2]


def fault(name: str) -> int:
    return FAULTS.index(name) + 1


def run(scen, choices, tmp_path):
    return runner.execute(scen, {"explicit": choices}, tmp_path / "w")


# ------------------------------------------------------------------------------ hidden arguments


def test_framework_injected_arguments_are_not_rendered_or_logged(tmp_path):
    out = run(sc.calls_a_framework_tool, {}, tmp_path)
    calls = [e["call"] for e in out.entries if e["kind"] == "effect"]
    assert calls[0] == "hook(order='1')"
    for hidden in ("config", "tool_call_id", "runtime", "secret", "hunter2"):
        assert not any(hidden in c for c in calls)
    effect = next(e for e in out.entries if e["kind"] == "effect")
    assert effect["args"] == {"order": "1"}


def test_hidden_arguments_do_not_change_what_counts_as_the_same_call(tmp_path):
    out = run(sc.hidden_args_do_not_enter_the_approval_hash, {}, tmp_path)
    assert out.ok  # the approval names the business arguments only


# --------------------------------------------------------------------------------- root cause


def wrapped(inner: BaseException, outer: BaseException) -> BaseException:
    try:
        raise outer from inner
    except BaseException as exc:  # noqa: BLE001
        return exc


def test_the_injected_exception_beats_the_framework_wrapper():
    injected = rootcause.mark_injected(TimeoutError("shape: injected timeout"))
    exc = wrapped(injected, RuntimeError("tool node failed"))
    assert rootcause.describe(exc) == (
        "TimeoutError: shape: injected timeout (surfaced as RuntimeError: tool node failed)")


def test_without_an_injected_exception_the_innermost_one_is_shown():
    exc = wrapped(wrapped(KeyError("sku"), ValueError("bad row")), RuntimeError("tool failed"))
    assert rootcause.describe(exc).startswith("KeyError: 'sku' (surfaced as RuntimeError")


def test_exception_groups_and_implicit_context_are_walked():
    try:
        try:
            raise ConnectionResetError("peer")
        except ConnectionResetError:
            raise RuntimeError("during handling")
    except RuntimeError as exc:
        assert rootcause.describe(exc).startswith("ConnectionResetError: peer")
    group = ExceptionGroup("tasks", [TimeoutError("slow")])
    assert rootcause.describe(group).startswith("TimeoutError: slow (surfaced as ExceptionGroup")


def test_a_note_a_framework_added_is_not_taken_for_the_error():
    exc = TimeoutError("read timed out")
    exc.add_note("During task with name 'tools' and id '1'")
    assert rootcause.describe(exc) == "TimeoutError: read timed out"


def test_a_died_incarnation_logs_the_root_cause_not_the_wrapper(tmp_path):
    out = run(sc.wrapped_tool_failure, {"fault:t.refund#0.0": fault("timeout")}, tmp_path)
    text = next(e["text"] for e in out.entries if e["kind"] == "error")
    assert text == ("TimeoutError: shape: injected timeout "
                    "(surfaced as RuntimeError: tool node failed)")
    trace, _ = runner.story(out.entries)
    assert any("process raised: TimeoutError: shape: injected timeout" in t for t in trace)


# ----------------------------------------------------------------------------- distinct causes


def test_failing_seeds_are_grouped_by_root_cause_with_counts(tmp_path):
    swept = runner.run_sweep(sc.two_independent_model_bugs, range(300), tmp_path)
    report = runner.investigate_all(sc.two_independent_model_bugs, swept.failing, tmp_path, "x")
    signatures = {c.failure.signature for c in report.causes}
    assert ("at_most_once(t.refund)", "model:after_refund=1") in signatures
    assert ("at_most_once(t.pay)", "model:after_pay=1") in signatures
    assert sum(len(c.seeds) for c in report.causes) == len(swept.failing)
    assert any(len(c.seeds) > 1 for c in report.causes)
    text = report.render()
    assert f"{len(report.causes)} distinct causes across {len(swept.failing)} failing seeds" in text
    assert text.count("\nFAIL ") == len(report.causes)  # one minimized trace per cause


def test_one_invariant_with_two_different_minimal_runs_is_two_causes(tmp_path):
    swept = runner.run_sweep(sc.naive_retry, range(300), tmp_path)
    report = runner.investigate_all(sc.naive_retry, swept.failing, tmp_path, "x")
    invariants = {c.failure.invariant for c in report.causes}
    assert invariants == {"at_most_once(t.pay)"}
    # Reply-lost faults (timeout_after_commit, connection_reset_after_commit) group separately
    # from process death after commit (crash_after) because they need different fixes.
    assert len(report.causes) == 2
    rendered = report.render()
    assert "reply lost" in rendered
    assert "process died after the commit" in rendered
    assert "timeout_after_commit" in rendered and "crash_after" in rendered


def test_the_shrink_summary_says_deviations_not_faults(tmp_path):
    with pytest.raises(AssertionError) as info:
        shape.check(sc.naive_retry, seeds=200, runs_dir=tmp_path)
    text = str(info.value)
    assert "deviations kept" in text
    assert "faults kept" not in text
    assert "distinct cause" in text


# -------------------------------------------------------------------- coverage and vacuous passes


def test_a_sweep_reports_what_it_exercised(tmp_path):
    swept = runner.run_sweep(sc.model_never_repeats, range(200), tmp_path)
    assert swept.cov.effects["t.keyed_refund"] == 200
    assert swept.cov.attempts["t.keyed_refund"] >= 200
    assert sum(swept.cov.faults["t.keyed_refund"].values()) > 0
    assert swept.cov.choices  # some seed took a non-nominal model choice
    text = swept.cov.render()
    assert "coverage over 200 seeds:" in text and "t.keyed_refund" in text
    assert "faults injected:" in text and "model choices taken: after_refund -> " in text


def test_a_scenario_that_never_reaches_its_tools_is_not_a_pass(tmp_path):
    with pytest.raises(AssertionError) as info:
        shape.check(sc.scenario_with_no_tool_calls, seeds=20, runs_dir=tmp_path)
    text = str(info.value)
    assert "NOTHING TESTED" in text
    assert "no effect was called" in text


def test_a_scenario_that_must_do_nothing_can_say_so(tmp_path):
    shape.check(sc.scenario_that_must_do_nothing, seeds=20, runs_dir=tmp_path)


def test_a_watched_op_no_seed_called_is_noted(tmp_path):
    swept = runner.run_sweep(sc.mail_with_approval, range(5), tmp_path)
    assert swept.notes(sc.mail_with_approval) == []
    swept = runner.run_sweep(sc.scenario_that_must_do_nothing, range(5), tmp_path)
    assert swept.notes(sc.scenario_that_must_do_nothing) == [
        "note: an invariant watches t.pay, which no seed ever called"]


def test_the_cli_prints_the_coverage_table_and_fails_a_vacuous_pass(tmp_path, capsys):
    scenarios = ROOT / "tests" / "boundary" / "scenarios.py"
    code = cli.main(["test", f"{scenarios}::scenario_with_no_tool_calls", "--seeds", "10",
                     "--runs-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "coverage over 10 seeds:" in out and "NOTHING TESTED" in out
    assert "PASS" not in out


# --------------------------------------------------------------------------------- flaky seeds


def test_a_seed_that_does_not_fail_again_is_flaky_not_a_failure(tmp_path):
    flag = tmp_path / "flag"

    @shape.scenario(invariants=[shape.at_most_once("t.pay")], max_restarts=0)
    def flaky(run):
        # not controlled by shape: the first execution on this machine double pays, re-runs do not
        first = not flag.exists()
        flag.write_text("seen")
        for _ in range(2 if first else 1):
            sc.pay_unkeyed_twice()

    swept = runner.run_sweep(flaky, range(1), tmp_path / "runs")
    assert swept.failing == [] and swept.flaky == [0]
    with pytest.raises(AssertionError) as info:
        flag.unlink()
        shape.check(flaky, seeds=1, runs_dir=tmp_path / "runs2")
    text = str(info.value)
    assert "1 flaky" in text and "FLAKY: 1 seeds failed once" in text
    assert "minimized to" not in text  # a flaky seed is never shrunk silently


def test_a_failure_that_reproduces_every_time_is_not_flaky(tmp_path):
    swept = runner.run_sweep(sc.naive_retry, range(200), tmp_path)
    assert swept.failing and swept.flaky == []


def test_a_seed_whose_replay_differs_is_reported_not_minimized(tmp_path):
    with pytest.raises(AssertionError) as info:
        shape.check(sc.replays_differ_from_seeds, seeds=1, runs_dir=tmp_path)
    text = str(info.value)
    assert "NOT MINIMIZED" in text and "did not fail again" in text
    assert "minimized to" not in text


# ------------------------------------------------------------------------------ clean CLI exits


def test_a_scenario_file_that_does_not_import_gets_one_clean_message(tmp_path, capsys):
    bad = tmp_path / "checks.py"
    bad.write_text("import shape\nimport no_such_framework_xyz\n")
    code = cli.main(["test", str(bad), "--runs-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "ModuleNotFoundError: No module named 'no_such_framework_xyz'" in err
    assert "(checks.py:2)" in err
    assert "Traceback" not in err


def test_a_syntax_error_and_a_missing_file_are_clean_too(tmp_path, capsys):
    bad = tmp_path / "broken.py"
    bad.write_text("def (:\n")
    assert cli.main(["test", str(bad)]) == 2
    err = capsys.readouterr().err
    assert "SyntaxError" in err and "(broken.py:1)" in err and "Traceback" not in err
    assert cli.main(["test", str(tmp_path / "nope.py")]) == 2
    assert "no such file" in capsys.readouterr().err


def test_a_closed_stdout_pipe_exits_quietly(tmp_path):
    scenarios = ROOT / "tests" / "boundary" / "scenarios.py"
    proc = subprocess.Popen(
        [sys.executable, "-m", "shape.cli", "test", f"{scenarios}::naive_retry", "--seeds", "20",
         "--runs-dir", str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
    proc.stdout.close()  # the reader (`| head -1`) went away before the report was printed
    stderr = proc.stderr.read()
    proc.stderr.close()
    assert proc.wait() == 141
    assert stderr == b""
