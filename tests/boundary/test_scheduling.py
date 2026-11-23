"""Both boundary schedulers use the same replay and shrink path as faults."""

import asyncio
import json
import threading
from pathlib import Path

import pytest

import shape
from shape.boundary import runner
from shape.boundary.child import load_module
from shape.boundary.faults import DUPLICATE_KEY, FAULTS

ACTORS = "sched:actors#0."


def execute(scenario, choices, workdir):
    return runner.execute(scenario, {"explicit": choices}, workdir)


@shape.effect("test.write", hide=["path"])
def write(path: Path, line: str) -> None:
    with path.open("a") as out:
        out.write(line + "\n")


def test_duplicate_delivery_interleaves_at_effect_calls_and_replays(tmp_path):
    @shape.scenario(duplicate_trigger=True, max_restarts=0)
    def delivered(run):
        write(run.dir / "writes", "first")
        write(run.dir / "writes", "second")

    choices = {DUPLICATE_KEY: 1, f"{ACTORS}1": 1}
    first = execute(delivered, choices, tmp_path / "first")
    second = execute(delivered, first.realized, tmp_path / "second")
    effects = [e for e in first.entries if e["kind"] == "effect"]
    assert [e["twin"] for e in effects] == [0, 1, 0, 1]
    assert first.entries == second.entries
    assert second.realized == choices
    assert any("actor 1 delivered at" in line for line in runner.story(first.entries)[0])


def test_shrink_removes_an_irrelevant_actor_switch(tmp_path):
    def writer(run):
        shape.point("write value")
        (run.dir / "value").touch()
        shape.point("writer cleanup")

    @shape.invariant
    def snapshot_is_valid(world):
        if (world.run.dir / "race").exists():
            return "the value changed after the snapshot"
        return None

    @shape.scenario(concurrent=[writer], invariants=[snapshot_is_valid], expect_effects=False)
    def reader(run):
        shape.point("read value")
        present = (run.dir / "value").exists()
        shape.point("use snapshot")
        if present != (run.dir / "value").exists():
            (run.dir / "race").touch()
        shape.point("reader cleanup")

    cause, noise = f"{ACTORS}1", f"{ACTORS}3"
    original = execute(reader, {cause: 1, noise: 1}, tmp_path / reader.name / "seed-42")
    assert original.violated(snapshot_is_valid.name)
    assert original.realized == {cause: 1, noise: 1}
    failure = runner.investigate(
        reader, 42, snapshot_is_valid.name, original.realized, tmp_path, reader.name)
    assert failure.problem is None
    assert json.loads(failure.replay_file.read_text())["realized"] == {cause: 1}
    code, trace = runner.replay_file(reader, failure.replay_file, tmp_path)
    assert code == 1 and " (replayed)" in trace
    assert "writer at 'write value' before actor 0 reader at 'use snapshot'" in trace


@pytest.mark.parametrize(("file", "name"), [("lead_summary", "summary"), ("job_claim", "queue")])
def test_seed_sweep_finds_the_race_and_passes_the_fixed_version(file, name, tmp_path):
    examples = Path(__file__).resolve().parents[2] / "examples" / "races"
    module = load_module(examples / f"{file}.py")
    buggy = getattr(module, f"{name}_buggy")
    fixed = getattr(module, f"{name}_fixed")
    sweep = runner.run_sweep(buggy, range(24), tmp_path, jobs=2)
    assert sweep.failing and not sweep.flaky
    assert sweep.cov.schedules
    assert "interleaving choices taken: actors:" in sweep.cov.render()
    assert not runner.run_sweep(fixed, range(24), tmp_path, jobs=2).failing


@pytest.mark.parametrize("fault", ["crash_before", "crash_after"])
def test_crashed_actor_restarts_using_its_own_fault_keys(fault, tmp_path):
    def peer(run):
        if not (run.dir / "peer").exists():
            write(run.dir / "peer", "landed")
        (run.dir / f"peer-completed-{run.restarts}").touch()

    @shape.scenario(concurrent=[peer], max_restarts=1)
    def primary(run):
        shape.point("record completion")
        (run.dir / f"primary-completed-{run.restarts}").touch()

    choices = {f"{ACTORS}0": 1, "fault:test.write#0p1.0": FAULTS.index(fault) + 1}
    out = execute(primary, choices, tmp_path / "run")
    assert out.ok and out.completed
    assert out.realized == choices
    assert (out.dir / "primary-completed-0").exists()
    assert (out.dir / "primary-completed-1").exists()
    assert not (out.dir / "peer-completed-0").exists()
    assert (out.dir / "peer-completed-1").exists()
    assert (out.dir / "peer").read_text() == "landed\n"
    assert [(e["actor_id"], e["inc"], e["how"]) for e in out.entries
            if e["kind"] == "incarnation" and e["how"] == "crash"] == [(1, 0, "crash")]


def test_a_blocked_actor_reports_the_deadlock_and_parked_peer(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "INCARNATION_TIMEOUT", 0.4)

    def peer(run):
        shape.point("release client")
        (run.dir / "release").touch()

    @shape.scenario(concurrent=[peer], max_restarts=0, expect_effects=False)
    def blocked(run):
        shape.point("wait for client")
        threading.Event().wait()

    out = execute(blocked, {}, tmp_path / "run")
    assert not out.completed
    assert len(out.violations) == 1 and out.violations[0].invariant == "scenario_terminates"
    assert "possible deadlock or timeout: blocked" in out.violations[0].message
    assert "'wait for client'" in out.violations[0].message
    assert "other actors waiting: peer at 'release client'" in out.violations[0].message
    assert not (out.dir / "release").exists()


def test_run_async_explores_gather_orders_and_replays(tmp_path):
    @shape.scenario(max_restarts=0, expect_effects=False)
    def gather(run):
        order = []

        async def left():
            order.append("left")

        async def right():
            order.append("right")

        async def main():
            await asyncio.gather(left(), right())

        shape.run_async(main())
        (run.dir / "order").write_text(" ".join(order))

    orders = set()
    for seed in range(40):
        first = runner.execute(gather, {"seed": seed}, tmp_path / "seed")
        order = (first.dir / "order").read_text()
        orders.add(order)
        replay = execute(gather, first.realized, tmp_path / "replay")
        assert first.ok and replay.ok
        assert (replay.dir / "order").read_text() == order
        assert first.entries == replay.entries
        if order == "right left":
            assert any(k.startswith("sched:tasks#0.") for k in first.realized)
            assert any("(right) before callback 0 (left)" in s
                       for s in runner.story(first.entries)[0])
        if len(orders) == 2:
            break
    assert orders == {"left right", "right left"}


def test_run_async_has_virtual_timers_and_a_real_executor(tmp_path):
    @shape.scenario(max_restarts=0, expect_effects=False)
    def scenario(run):
        async def main():
            loop = asyncio.get_running_loop()
            await asyncio.sleep(3600)
            assert loop.time() == 3600
            barrier = threading.Barrier(2)
            await asyncio.gather(*(asyncio.to_thread(barrier.wait, 2) for _ in range(2)))
            result = loop.create_future()
            await asyncio.to_thread(loop.call_soon_threadsafe, result.set_result, 42)
            return await result

        assert shape.run_async(main()) == 42

    assert execute(scenario, {}, tmp_path / "run").ok


def test_run_async_reports_an_idle_deadlock(tmp_path):
    @shape.scenario(max_restarts=0, expect_effects=False)
    def deadlocked(run):
        async def main():
            await asyncio.get_running_loop().create_future()

        shape.run_async(main())

    out = execute(deadlocked, {}, tmp_path / "run")
    assert "Deadlock: nothing runnable, no timers" in out.violations[0].message


def test_failing_scheduled_seed_can_still_be_flaky(tmp_path):
    marker = tmp_path / "ran-before"

    def peer(run):
        shape.point("peer")

    @shape.scenario(concurrent=[peer], max_restarts=0, expect_effects=False)
    def scenario(run):
        shape.point("first-time check")
        if not marker.exists():
            marker.touch()
            raise RuntimeError("only fails once")

    sweep = runner.run_sweep(scenario, range(1), tmp_path / "runs")
    assert sweep.flaky == [0]
    assert sweep.failing == []
