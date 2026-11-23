"""client_error, duplicate_trigger and world actors."""

import json
from pathlib import Path

import pytest
import scenarios as sc

import shape
from shape.boundary import runner
from shape.boundary.faults import DUPLICATE_KEY, FAULTS


def fault(name: str) -> int:
    return FAULTS.index(name) + 1


def run(scen, choices, tmp_path):
    return runner.execute(scen, {"explicit": choices}, tmp_path / "w")


def test_client_error_raises_shape_client_error_before_the_effect(tmp_path):
    out = run(sc.client_error_uncaught, {"fault:t.refund#0.0": fault("client_error")}, tmp_path)
    assert [e for e in out.entries if e["kind"] == "effect"] == []
    error = next(e for e in out.entries if e["kind"] == "error")
    assert error["text"] == "shape.boundary.faults.ClientError: shape: injected client_error"
    trace, _ = runner.story(out.entries)
    assert any("client error (4xx) before the effect: the caller saw ClientError" in t
               for t in trace)
    assert shape.ClientError is not shape.ServerError


def test_client_error_exception_is_the_tools_own_when_configured(tmp_path):
    class Declined(Exception):
        pass

    @shape.effect("t.cfg", errors={"client_error": lambda: Declined("402")})
    def cfg():
        pass

    @shape.scenario(max_restarts=0)
    def uses_cfg(run):
        cfg()

    out = run_explicit(uses_cfg, {"fault:t.cfg#0.0": fault("client_error")}, tmp_path)
    assert "Declined: 402" in next(e for e in out.entries if e["kind"] == "error")["text"]


def run_explicit(scen, choices, tmp_path):
    return runner.execute(scen, {"explicit": choices}, tmp_path / "w")


def test_client_error_is_drawn_by_seeds(tmp_path):
    swept = runner.run_sweep(sc.model_never_repeats, range(300), tmp_path)
    assert swept.cov.faults["t.keyed_refund"]["client_error"] > 0


def test_duplicate_trigger_starts_the_first_incarnation_twice_on_the_same_dir(tmp_path):
    out = run(sc.webhook_handler_without_dedup, {DUPLICATE_KEY: 1}, tmp_path)
    effects = [e for e in out.entries if e["kind"] == "effect"]
    assert sorted(e["twin"] for e in effects) == [0, 1]
    assert [v.invariant for v in out.violations] == ["at_most_once(t.hook_pay)"]
    assert (out.dir / "inc-0.out").exists() and (out.dir / "inc-0-t1.out").exists()


def test_duplicate_trigger_trace_shows_both_copies(tmp_path):
    out = run(sc.webhook_handler_without_dedup, {DUPLICATE_KEY: 1}, tmp_path)
    trace, _ = runner.story(out.entries)
    text = "\n".join(trace)
    assert "incarnation 0 (copy A of the duplicate delivery)" in text
    assert "incarnation 0 (copy B of the duplicate delivery)" in text
    assert "delivered twice: two copies ran at the same time on the same run dir" in text


def test_duplicate_trigger_replays_deterministically(tmp_path):
    first = run(sc.webhook_handler_without_dedup, {DUPLICATE_KEY: 1}, tmp_path / "a")
    again = run(sc.webhook_handler_without_dedup, {DUPLICATE_KEY: 1}, tmp_path / "b")
    assert runner.story(first.entries) == runner.story(again.entries)
    assert first.realized == again.realized == {DUPLICATE_KEY: 1}


def test_twin_draw_keys_are_namespaced_per_copy(tmp_path):
    only_copy_b = {DUPLICATE_KEY: 1, "fault:t.hook_pay#0t1.0": fault("timeout")}
    out = run(sc.webhook_handler_without_dedup, only_copy_b, tmp_path)
    faults = [e for e in out.entries if e["kind"] == "fault" and e["op"] == "t.hook_pay"]
    assert [(e["twin"], e["fault"]) for e in faults] == [(1, "timeout")]
    assert out.realized == only_copy_b
    # copy A's call was untouched, copy B's raised before commit: one effect, no duplicate
    assert out.ok


def test_duplicate_trigger_is_found_by_seeds_and_shrunk_to_the_delivery(tmp_path):
    with pytest.raises(AssertionError) as info:
        shape.check(sc.webhook_handler_without_dedup, seeds=300, runs_dir=tmp_path)
    text = str(info.value)
    assert "invariant=at_most_once(t.hook_pay)" in text
    assert "duplicate trigger:" in text
    minimal = [json.loads(f.read_text())["realized"]
               for f in (tmp_path / "webhook_handler_without_dedup").glob("seed-*.min.json")]
    assert {DUPLICATE_KEY: 1} in minimal  # the delivery alone, every injected fault shrunk away


def test_an_idempotency_key_makes_the_scenario_survive_duplicate_delivery(tmp_path):
    shape.check(sc.webhook_handler_with_dedup, seeds=300, runs_dir=tmp_path)


def test_duplicate_trigger_is_opt_in_per_scenario(tmp_path):
    swept = runner.run_sweep(sc.one_shot_handler, range(300), tmp_path)
    assert "(scenario)" not in swept.cov.faults
    swept = runner.run_sweep(sc.webhook_handler_with_dedup, range(300), tmp_path)
    assert swept.cov.faults["(scenario)"]["duplicate_trigger"] > 0


def test_duplicate_trigger_with_the_spawn_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAPE_START_METHOD", "spawn")
    sc.webhook_handler_without_dedup.file = str(Path(sc.__file__))
    out = run(sc.webhook_handler_without_dedup, {DUPLICATE_KEY: 1}, tmp_path)
    assert sorted(e["twin"] for e in out.entries if e["kind"] == "effect") == [0, 1]


def test_a_world_actor_that_edits_state_between_incarnations_finds_the_stale_approval(tmp_path):
    out = run(sc.stale_approval, {"model:user_edits_order#0a.0": 1}, tmp_path)
    assert [v.invariant for v in out.violations] == ["approved_before(t.send)"]
    trace, _ = runner.story(out.entries)
    assert any("model chose 'raise the amount' at 'user_edits_order' (nominal: 'leave it')"
               " [world actor edit_amount_choice]" in t for t in trace)


def test_a_world_actor_that_leaves_things_alone_passes_and_runs_between_incarnations_only(tmp_path):
    out = run(sc.stale_approval, {}, tmp_path)
    assert out.ok
    # incarnation 0 died, the actor ran once, incarnation 1 finished: no actor call after "done"
    assert (out.dir / "actor.log").read_text() == "ran\n"


def test_world_actor_choices_are_found_by_seeds_and_shrink_to_the_one_edit(tmp_path):
    with pytest.raises(AssertionError) as info:
        shape.check(sc.stale_approval, seeds=300, runs_dir=tmp_path)
    text = str(info.value)
    assert "[world actor edit_amount_choice]" in text
    replay = next(line for line in text.splitlines() if line.startswith("  replay: "))
    min_file = Path(replay.removeprefix("  replay: "))
    assert json.loads(min_file.read_text())["realized"] == {"model:user_edits_order#0a.0": 1}


def test_new_faults_are_appended_so_saved_replay_files_keep_their_meaning():
    # a replay file stores indexes into this table; client_error went last, after crash_after
    assert FAULTS[:7] == ("timeout", "timeout_after_commit", "connection_reset_after_commit",
                          "rate_limit", "server_error", "crash_before", "crash_after")
    assert FAULTS[7] == "client_error"
