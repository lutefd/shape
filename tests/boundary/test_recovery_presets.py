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


@pytest.mark.parametrize(("preset", "name", "label"), [
    ("redeliver", "redeliver", "redelivered the request"),
    ("cron-retick", "cron-retick", "cron re-ticked the job"),
    (shape.recovery.APSchedulerReload(interval=60), "apscheduler-reload",
     "APScheduler reloaded the job store; the crashed tick was not rerun"),
    (shape.recovery.CeleryAcksLate(reject_on_worker_lost=True), "celery-acks-late",
     "Celery redelivered the unacknowledged task"),
    ("lease-takeover", "lease-takeover", "another worker reclaimed the job after its lease expired"),
])
def test_presets_rerun_a_plain_python_job_after_crash(tmp_path, preset, name, label):
    scenario = dataclasses.replace(run_job, recovery=preset)
    fault = FAULTS.index("crash_after") + 1
    result = runner.execute(scenario, {"explicit": {"fault:jobs.write#0.0": fault}}, tmp_path)
    assert result.violated("at_most_once(jobs.write)")
    lines, _ = runner.story(result.entries)
    assert f"  harness: {label} (recovery={name!r})" in lines


@pytest.mark.parametrize("preset", [
    "apscheduler-reload",  # a one-shot job is removed from the store when submitted
    "celery-acks-late",  # acknowledged when its pool child dies, unless reject_on_worker_lost
])
def test_presets_that_do_not_bring_a_crashed_job_back_by_default(tmp_path, preset):
    scenario = dataclasses.replace(run_job, recovery=preset)
    fault = FAULTS.index("crash_after") + 1
    result = runner.execute(scenario, {"explicit": {"fault:jobs.write#0.0": fault}}, tmp_path)
    assert [v.invariant for v in result.violations] == ["scenario_terminates"]
    assert result.stats["effects"] == {"jobs.write": 1}
    assert not (tmp_path / "inc-1.out").exists()


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


def test_celery_redelivers_a_killed_task_only_with_reject_on_worker_lost(tmp_path):
    def killed(run):
        if not run.restarts:
            os.kill(os.getpid(), 9)

    def scenario(recovery):
        return shape.scenario(killed, recovery=recovery, max_restarts=1)

    default = runner.execute(scenario("celery-acks-late"), {"explicit": {}}, tmp_path / "a")
    assert default.violated("scenario_terminates")
    rejecting = shape.recovery.CeleryAcksLate(reject_on_worker_lost=True)
    assert runner.execute(scenario(rejecting), {"explicit": {}}, tmp_path / "b").ok
    whole_worker = shape.recovery.CeleryAcksLate(lost="worker")
    assert runner.execute(scenario(whole_worker), {"explicit": {}}, tmp_path / "c").ok


def test_recovery_rejects_typos_at_setup():
    with pytest.raises(ValueError, match="unknown recovery preset"):
        shape.scenario(recovery="cron")
