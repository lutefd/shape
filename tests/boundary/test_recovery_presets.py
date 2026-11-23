import dataclasses
import os

import pytest

import shape
from shape.boundary import runner
from shape.boundary.faults import FAULTS


@shape.effect("jobs.write", scope=lambda: "job:17")
def write():
    pass


@shape.scenario(invariants=[shape.at_most_once("jobs.write")], max_restarts=1)
def run_job(run):
    write()


@pytest.mark.parametrize(("preset", "label"), [
    ("redeliver", "redelivered the request"),
    ("cron-retick", "cron re-ticked the job"),
    ("apscheduler-reload", "APScheduler reloaded and ran the job"),
    ("celery-acks-late", "Celery redelivered the unacknowledged task"),
])
def test_presets_rerun_a_plain_python_job_after_crash(tmp_path, preset, label):
    scenario = dataclasses.replace(run_job, recovery=preset)
    fault = FAULTS.index("crash_after") + 1
    result = runner.execute(scenario, {"explicit": {"fault:jobs.write#0.0": fault}}, tmp_path)
    assert result.violated("at_most_once(jobs.write)")
    lines, _ = runner.story(result.entries)
    assert f"  harness: {label} (recovery={preset!r})" in lines


def test_none_preserves_crash_as_termination_failure(tmp_path):
    scenario = dataclasses.replace(run_job, recovery="none")
    fault = FAULTS.index("crash_after") + 1
    result = runner.execute(scenario, {"explicit": {"fault:jobs.write#0.0": fault}}, tmp_path)
    assert [v.invariant for v in result.violations] == ["scenario_terminates"]
    assert result.stats["effects"] == {"jobs.write": 1}
    assert not (tmp_path / "inc-1.out").exists()


def test_celery_does_not_redeliver_a_task_that_raised(tmp_path):
    @shape.scenario(recovery="celery-acks-late", max_restarts=1)
    def raises(run):
        raise RuntimeError("failed task")

    result = runner.execute(raises, {"explicit": {}}, tmp_path)
    assert result.violated("scenario_terminates")
    assert not (tmp_path / "inc-1.out").exists()


def test_celery_redelivers_after_worker_signal(tmp_path):
    @shape.scenario(recovery="celery-acks-late", max_restarts=1)
    def killed(run):
        if not run.restarts:
            os.kill(os.getpid(), 9)

    assert runner.execute(killed, {"explicit": {}}, tmp_path).ok


def test_recovery_rejects_typos_at_setup():
    with pytest.raises(ValueError, match="unknown recovery preset"):
        shape.scenario(recovery="cron")
