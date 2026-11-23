"""`celery-acks-late` against a real Celery worker (in-process, in-memory broker, prefork pool): a
task process killed mid-task is redelivered only with task_reject_on_worker_lost, and a task that
raised is acknowledged. The preset must predict the same thing."""

import os
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import shape
from shape.boundary import runner
from shape.boundary.recovery import CeleryAcksLate, Died

pytest.importorskip("celery")
pytest.importorskip("billiard")

WORKER = Path(__file__).parent / "real" / "celery_worker.py"


def celery_runs(reject, how, deaths, expected):
    """How many times a real Celery worker starts a task that dies `deaths` times."""
    out = subprocess.run(
        [sys.executable, str(WORKER), "reject" if reject else "default", how, str(deaths),
         str(expected)], capture_output=True, text=True, timeout=120, check=True)
    return int(out.stdout.split()[-1])


def predicted_runs(policy, outcome, deaths):
    """How many times the preset runs a task that dies `deaths` times with this outcome."""
    runs, inc = 1, 0
    while inc < deaths and policy.resume(Died(inc, outcome, 0.0, 0.0, 0.0)) is not None:
        runs += 1
        inc += 1
    return runs


CASES = [
    (False, "kill", "crash", 1, 1),  # acknowledged on worker loss: the task is gone
    (True, "kill", "crash", 1, 2),  # requeued, runs again
    (True, "kill", "crash", 2, 3),  # requeued every time (Celery 5.7 has no cap)
    (False, "raise", "error", 1, 1),  # raised: acknowledged, never redelivered
    (True, "raise", "error", 1, 1),  # reject_on_worker_lost is for worker loss only
]


@pytest.fixture(scope="module")
def real_celery():
    """Run every case against a real worker at once: each takes ten seconds to notice its death."""
    with ThreadPoolExecutor(len(CASES)) as pool:
        counts = list(pool.map(lambda c: celery_runs(c[0], c[1], c[3], c[4]), CASES))
    return dict(zip(CASES, counts, strict=True))


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"reject={c[0]}-{c[1]}-x{c[3]}")
def test_preset_matches_celery(real_celery, case):
    reject, _, outcome, deaths, runs = case
    assert real_celery[case] == runs, "Celery itself did not behave as the test expects"
    assert predicted_runs(CeleryAcksLate(reject_on_worker_lost=reject), outcome, deaths) == runs


def test_a_lost_worker_is_redelivered_without_reject_on_worker_lost():
    """Not run against Celery: killing the whole worker needs a real broker with redelivery."""
    rabbit = CeleryAcksLate(lost="worker")
    assert rabbit.resume(Died(0, "crash", 0.0, 0.0, 0.0)).advance == 0
    redis = CeleryAcksLate(lost="worker", broker="redis", visibility_timeout=900)
    assert redis.resume(Died(0, "crash", 0.0, 0.0, 0.0)).advance == 900


def test_shape_run_matches(tmp_path):
    @shape.effect("task.write", scope=lambda: "task")
    def write():
        pass

    @shape.scenario(max_restarts=2, recovery="celery-acks-late")
    def task(run):
        write()
        if not run.restarts:
            os.kill(os.getpid(), signal.SIGKILL)

    lost = runner.execute(task, {"explicit": {}}, tmp_path / "default")
    assert lost.violated("scenario_terminates")  # acknowledged on worker loss: not rerun
    assert len([e for e in lost.entries if e["kind"] == "effect"]) == 1

    import dataclasses
    rejected = dataclasses.replace(task, recovery=CeleryAcksLate(reject_on_worker_lost=True))
    again = runner.execute(rejected, {"explicit": {}}, tmp_path / "reject")
    assert again.ok
    assert len([e for e in again.entries if e["kind"] == "effect"]) == 2
