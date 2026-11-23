import time

import pytest

import shape
from shape import cli
from shape.boundary import runner


@shape.scenario(expect_effects=False)
def slow(run):
    time.sleep(10)


@shape.invariant
def always_fails(world):
    return "deliberate failure"


@shape.scenario(invariants=[always_fails])
def failing(run):
    pass


@pytest.mark.parametrize("jobs", [1, 2])
def test_budget_stops_slow_seeds_and_reports_incomplete(tmp_path, jobs):
    started = time.monotonic()
    with pytest.raises(AssertionError, match="skipped 2 of 2 requested seeds"):
        shape.check(slow, seeds=2, jobs=jobs, budget=0.1, runs_dir=tmp_path)
    assert time.monotonic() - started < 5


def test_max_shrinks_zero_keeps_failure_and_reports_no_shrink(tmp_path):
    with pytest.raises(AssertionError, match="2 more failing seeds were not minimized"):
        shape.check(failing, seeds=2, max_shrinks=0, runs_dir=tmp_path)
    assert (tmp_path / "failing" / "seed-0" / "world.jsonl").is_file()
    assert not list(tmp_path.rglob("*.min.json"))


def test_investigation_budget_reports_skipped_shrinks(tmp_path):
    swept = runner.run_sweep(failing, range(1), tmp_path)
    report = runner.investigate_all(failing, swept.failing, tmp_path, "test",
                                    deadline=runner.deadline_for(0))
    assert report.budget_skipped == 1
    assert "time budget expired" in report.render()


def test_cli_budget_and_max_shrinks(tmp_path, capsys):
    args = ["test", f"{__file__}::failing", "--runs-dir", str(tmp_path), "--seeds", "1"]
    assert cli.main([*args, "--budget", "0"]) == 1
    assert "INCOMPLETE: time budget exhausted" in capsys.readouterr().out
    assert cli.main([*args, "--max-shrinks", "0"]) == 1
    assert "1 more failing seeds were not minimized" in capsys.readouterr().out
