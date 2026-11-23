"""`apscheduler-reload` against the real library: a process that dies inside a job leaves the job
store behind, and a fresh scheduler on that store decides what runs next. The preset must predict
the same thing, for every misfire_grace_time and coalesce setting."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import shape
from shape.boundary import runner
from shape.boundary.recovery import APSchedulerReload, Died

apscheduler = pytest.importorskip("apscheduler")
pytest.importorskip("sqlalchemy")
if not apscheduler.__version__.startswith("3."):  # pragma: no cover - the preset models 3.x
    pytest.skip("the apscheduler-reload preset models APScheduler 3.x", allow_module_level=True)

WORKER = Path(__file__).parent / "real" / "aps_worker.py"


def library(tmp_path, interval, downtime, grace, coalesce):
    """What real APScheduler runs after a crash at t=0: [(scheduled, clock when it ran)]."""
    db = str(tmp_path / "jobs.db")
    crashed = subprocess.run([sys.executable, str(WORKER), "crash", db, "0", str(interval),
                              str(grace), str(coalesce).lower()], timeout=60, check=False)
    assert crashed.returncode == 137, "the job should have killed its own process"
    ticks = [] if interval == "date" else [k * interval for k in range(1, 5) if k * interval > downtime]
    out = subprocess.run([sys.executable, str(WORKER), "restart", db, "0", str(downtime), *map(str, ticks)],
                         capture_output=True, text=True, timeout=60, check=True)
    report = json.loads(out.stdout.splitlines()[-1])
    executed = [(sched, now) for kind, sched, now in report["events"] if kind == "executed"]
    return executed, report


def preset(interval, downtime, grace, coalesce):
    policy = APSchedulerReload(interval=None if interval == "date" else float(interval),
                               downtime=downtime,
                               misfire_grace_time=None if grace == "none" else float(grace),
                               coalesce=coalesce)
    resume = policy.resume(Died(0, "crash", 0.0, 0.0, 0.0))
    return None if resume is None else (resume.advance, list(resume.fires))


CASES = [
    # (interval, downtime, grace, coalesce, what happens)
    (60, 0, 1, True, "the crashed tick is not rerun; the next tick runs on time"),
    (60, 130, "none", True, "two ticks came due: coalesce delivers only the latest"),
    (60, 130, "none", False, "two ticks came due: without coalesce both run"),
    (60, 130, 15, True, "the latest due tick is within its grace time"),
    (60, 130, 5, True, "the latest due tick is past its grace time: missed, wait for the next"),
    (60, 130, 15, False, "the older tick is past its grace time, the newer one is not"),
    (60, 120, 1, True, "a tick due exactly at restart runs on time"),
    ("date", 0, 1, True, "a one-shot job is removed when submitted: it is never rerun"),
]


@pytest.mark.parametrize(("interval", "downtime", "grace", "coalesce", "why"), CASES,
                         ids=[c[4] for c in CASES])
def test_preset_matches_apscheduler(tmp_path, interval, downtime, grace, coalesce, why):
    executed, report = library(tmp_path, interval, downtime, grace, coalesce)
    expected = preset(interval, downtime, grace, coalesce)
    if not executed:
        assert expected is None, f"the library reran nothing but the preset restarts: {expected}"
        assert report["jobs"] == [] if interval == "date" else True
        return
    first = executed[0][1]
    observed = (first, [sched for sched, now in executed if now == first])
    assert expected is not None, f"the library reran at {observed} but the preset does not restart"
    assert (expected[0], expected[1]) == observed, why


def test_crashed_tick_never_runs_again_in_a_shape_scenario(tmp_path):
    """The same rule, end to end: the scenario is delivered only the ticks APScheduler would run."""
    @shape.effect("report.send", scope=lambda tick: f"tick:{tick}")
    def send(tick):
        pass

    @shape.scenario(max_restarts=2, invariants=[shape.at_most_once("report.send")],
                    recovery=APSchedulerReload(interval=60, downtime=130, misfire_grace_time=None,
                                               coalesce=False))
    def tick(run):
        send(run.scheduled if run.scheduled is not None else 0.0)
        if run.restarts == 0:
            import os

            os._exit(137)

    out = runner.execute(tick, {"explicit": {}}, tmp_path)
    ticks = [e["scope"] for e in out.entries if e["kind"] == "effect"]
    assert ticks == ["tick:0.0", "tick:60.0", "tick:120.0"]  # tick 0 is not rerun
