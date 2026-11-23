"""Runs a seed as a chain of incarnations (one forked process each), evaluates the invariants over
the world log, shrinks a failure by replaying realized draws through `ExplicitChoices`, and
renders it in the code's own words."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from ..choices import ChoiceSource, ExplicitChoices, SeededChoices
from ..shrink import shrink
from . import api, clock, coverage, fixtures, recovery, schedule
from .failure import (
    Defect,
    DefectKey,
    Failure,
    Report,
    describe_triggers,
    fault_class,
    scope_pattern,
    trigger_classes,
)
from .faults import CRASH_EXIT, DUPLICATE_KEY, DUPLICATE_TRIGGER, FAULT_TEXT, FAULTS
from .invariants import Violation, World, check_strict_scope, scenario_terminates
from .log import PARENT_SEQ, REALIZED, WORLD, JsonlWriter, event_order, read_jsonl
from .rootcause import describe

INCARNATION_TIMEOUT = 30.0  # seconds; a hung incarnation counts as one that never finished
TIMEOUT_EXIT = 124
HARNESS_EXIT = 78  # a user-supplied shape hook (scope/key/result/landed) raised
BUDGET_EXIT = 79
RERUNS = 2  # a failing seed must fail the same way this many more times to count as a failure
MAX_INVESTIGATED = 200  # failing seeds minimized per sweep, fewest deviations first


class BudgetExpired(Exception):
    pass


def deadline_for(budget: float | None) -> float | None:
    if budget is None:
        return None
    if not math.isfinite(budget) or budget < 0:
        raise ValueError("budget must be a finite, non-negative number of seconds")
    return time.monotonic() + budget


def _time_left(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BudgetExpired
    return remaining


@dataclass
class Outcome:
    dir: Path
    completed: bool
    entries: list[dict]
    realized: dict[str, int]
    violations: list[Violation]
    stats: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.violations

    def violated(self, name: str) -> bool:
        return any(v.invariant == name for v in self.violations)


def make_choices(spec: dict) -> ChoiceSource:
    return ExplicitChoices(spec["explicit"]) if "explicit" in spec else SeededChoices(spec["seed"])


def _network(scen: api.Scenario, workdir: Path):
    if scen.net is None:
        return contextlib.nullcontext()
    from ..net import _capture

    return _capture(workdir, scen.net)


# ---------------------------------------------------------------------------------- incarnations


def child_main(scen: api.Scenario, spec: dict, workdir: Path, inc: int, twin: int = 0, *,
               fn=None, gate: schedule.Gate | None = None, actor_slot: int = 0,
               deadline: float | None = None) -> int:
    """Run a scenario or its concurrent actor in an isolated process and return its exit code."""
    suffix = f"-p{actor_slot}" if actor_slot else f"-t{twin}" if twin else ""
    out = os.open(workdir / f"inc-{inc}{suffix}.out", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.dup2(out, 1)
    os.dup2(out, 2)
    if out > 2:
        os.close(out)
    sys.stdout = sys.stderr = os.fdopen(1, "w", buffering=1, closefd=False)
    # Fairness: faults eventually stop. With restarts allowed, the last incarnation runs fault-free,
    # so scenario_terminates asks whether the agent recovers, not whether the harness can outlast it.
    quiet = scen.max_restarts > 0 and inc == scen.max_restarts
    ctx = api._Context(workdir, inc, make_choices(spec), quiet, twin, gate=gate,
                       actor_slot=actor_slot, sync_logs=spec.get("sync_logs", True))
    api.activate(ctx)
    if gate is None:
        try:
            remaining = _time_left(deadline)
        except BudgetExpired:
            return BUDGET_EXIT
        timeout, exit_code = INCARNATION_TIMEOUT, TIMEOUT_EXIT
        if remaining is not None and remaining < timeout:
            timeout, exit_code = remaining, BUDGET_EXIT
        ctx.watchdog = threading.Timer(timeout, lambda: os._exit(exit_code))
        ctx.watchdog.daemon = True
        ctx.watchdog.start()
    try:
        if gate is not None:
            gate.enter()
        with _network(scen, workdir):
            policy = recovery.resolve(scen.recovery)
            if inc and actor_slot == 0 and policy.action:
                api._note(f"harness: {policy.action} (recovery={policy.name!r})")
            for tick in spec.get("fires") or [None]:
                if tick is not None and actor_slot == 0:
                    api._note(f"harness: tick scheduled for {clock.stamp(tick)}")
                (fn if fn is not None else scen.fn)(_run_for(scen, spec, workdir, inc, tick))
        return 0
    except api.HarnessError as exc:
        sys.stderr.write(f"harness error: {exc}\n")
        return HARNESS_EXIT
    except BaseException as exc:  # noqa: BLE001 - anything that kills the agent is a death to log
        ctx.append(ctx.world, {"kind": "error", "text": describe(exc)})
        sys.stderr.write(traceback.format_exc())
        return 1


def _run_for(scen: api.Scenario, spec: dict, workdir: Path, inc: int,
             scheduled: float | None = None) -> api.Run:
    return api.Run(workdir, inc, spec.get("seed"), recovery.name_of(scen.recovery),
                   spec.get("fixtures", {}), scheduled)


def _classify(code: int) -> str:
    if code < 0:
        return "crash"
    return {
        0: "done",
        CRASH_EXIT: "crash",
        TIMEOUT_EXIT: "timeout",
        HARNESS_EXIT: "harness_error",
    }.get(code, "error")


def _fork_incarnation(scen: api.Scenario, spec: dict, workdir: Path, inc: int,
                      deadline: float | None = None) -> int:
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        code = 70
        try:
            code = child_main(scen, spec, workdir, inc, deadline=deadline)
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def _spawn_incarnation(scen: api.Scenario, spec: dict, workdir: Path, inc: int,
                       deadline: float | None = None) -> int:
    root = str(Path(__file__).resolve().parents[2])
    paths = [root, *filter(None, [os.environ.get("PYTHONPATH")])]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(paths)}
    payload = json.dumps({"file": scen.file, "name": scen.name, "spec": spec,
                          "workdir": str(workdir), "inc": inc})
    try:
        return subprocess.run([sys.executable, "-m", "shape.boundary.child", payload],
                              env=env, timeout=_time_left(deadline), check=False).returncode
    except subprocess.TimeoutExpired as exc:
        raise BudgetExpired from exc


def _incarnate(scen: api.Scenario, spec: dict, workdir: Path, inc: int, copies: int,
               world: JsonlWriter, deadline: float | None = None) -> str:
    remaining = _time_left(deadline)
    use_fork = hasattr(os, "fork") and os.environ.get("SHAPE_START_METHOD") != "spawn"
    if copies == 1 and not scen.concurrent:
        run = _fork_incarnation if use_fork else _spawn_incarnation
        code = run(scen, spec, workdir, inc, deadline)
        if code == BUDGET_EXIT:
            raise BudgetExpired
        outcome = _classify(code)
        world.write({"kind": "incarnation", "inc": inc, "twin": 0, "seq": PARENT_SEQ,
                     "how": outcome})
        return outcome
    budgeted = remaining is not None and remaining < INCARNATION_TIMEOUT
    actors = schedule.run_processes(
        scen, spec, workdir, inc, copies, use_fork=use_fork, choices=make_choices(spec),
        quiet=scen.max_restarts > 0 and inc == scen.max_restarts,
        timeout=remaining if budgeted else INCARNATION_TIMEOUT,
        timeout_exit=TIMEOUT_EXIT, world=world)
    codes = [a.code for a in actors]
    if budgeted and TIMEOUT_EXIT in codes:
        raise BudgetExpired
    for a in actors:
        world.write(a.event(inc, PARENT_SEQ, kind="incarnation", how=_classify(a.code)))
    if HARNESS_EXIT in codes:
        return "harness_error"
    if (0 in codes[:copies] and all(code == 0 for code in codes[copies:])
            and TIMEOUT_EXIT not in codes):
        return "done"
    if TIMEOUT_EXIT in codes:
        return "timeout"
    if any(_classify(code) == "crash" for code in codes):
        return "crash"
    return "error"


def _delivered_twice(scen: api.Scenario, spec: dict, workdir: Path) -> bool:
    """Draw whether this seed delivers the trigger twice: incarnation 0 starts as two copies."""
    if not scen.duplicate_trigger:
        return False
    choices = make_choices(spec)
    weights = [1.0, 1.0 if choices.enabled(DUPLICATE_TRIGGER) else 0.0]
    if not choices.draw(DUPLICATE_KEY, weights):
        return False
    log = JsonlWriter(workdir / REALIZED, fsync=spec.get("sync_logs", True))
    log.write({"key": DUPLICATE_KEY, "value": 1, "fault": DUPLICATE_TRIGGER,
               "op": coverage.SCENARIO_OP, "call": "the scenario is triggered", "raised": None,
               "inc": 0, "twin": 0, "seq": 0})
    log.close()
    return True


def _run_actors(scen: api.Scenario, spec: dict, workdir: Path, inc: int) -> bool:
    """Call the world actors in the parent, between incarnation `inc` and the next one."""
    if not scen.actors:
        return True
    ctx = api._Context(workdir, inc, make_choices(spec), actors=True,
                       sync_logs=spec.get("sync_logs", True))
    api.activate(ctx)
    try:
        with _network(scen, workdir):
            for act in scen.actors:
                ctx.actor = act.name
                try:
                    act(_run_for(scen, spec, workdir, inc + 1))
                except api.HarnessError:
                    return False
                except Exception as exc:  # noqa: BLE001 - a broken actor is reported, not fatal
                    ctx.append(ctx.world, {"kind": "error",
                                           "text": f"world actor {act.name} raised {describe(exc)}"})
    finally:
        api.activate(None)
        ctx.close()
    return True


def _run_setup(scen: api.Scenario, spec: dict, workdir: Path) -> bool:
    """Run the scenario's setup phase in the parent: no faults, no crashes, no model choices. Its
    effects are logged as setup. False means setup failed and the run must not start."""
    if scen.setup is None:
        return True
    ctx = api._Context(workdir, -1, make_choices(spec), quiet=True, setup=True,
                       sync_logs=spec.get("sync_logs", True))
    api.activate(ctx)
    try:
        with _network(scen, workdir):
            try:
                scen.setup(_run_for(scen, spec, workdir, 0))
            except api.HarnessError:
                return False
            except Exception as exc:  # noqa: BLE001 - a broken setup is reported, not fatal
                api._harness_fail_setup(ctx, scen.setup, exc)
                return False
    finally:
        api.activate(None)
        ctx.close()
    return True


# ------------------------------------------------------------------------------------------ a seed


def execute(scen: api.Scenario, spec: dict, workdir: Path, *, deadline: float | None = None,
            collect_stats: bool = True) -> Outcome:
    """Run one seed (or one explicit replay) in `workdir`, restarting after every death. The
    scenario's fixtures live from before its setup until after its invariants have read the world."""
    stack = contextlib.ExitStack()
    try:
        out = _execute(scen, spec, workdir, stack, deadline, collect_stats)
    except BaseException:
        with contextlib.suppress(Exception):
            stack.close()
        raise
    try:
        stack.close()
    except Exception as exc:  # noqa: BLE001 - a fixture that cannot clean up is the user's to fix
        out.violations.append(Violation(
            "harness_error", f"a fixture's teardown raised {describe(exc)}", None))
    return out


def _fixture_failed(workdir: Path, exc: BaseException) -> None:
    with contextlib.closing(JsonlWriter(workdir / WORLD, fsync=False)) as log:
        log.write({"kind": "harness_error", "op": "fixture", "hook": "fixture", "inc": -1,
                   "twin": 0, "seq": 0, "text": f"your fixture raised {describe(exc)}"})


def _pass_time(parent: JsonlWriter, workdir: Path, inc: int, seconds: float) -> None:
    """Let virtual time pass before incarnation `inc` starts, and show it in the trace."""
    if seconds <= 0:
        return
    total = clock.advance_dir(workdir, seconds)
    parent.write({"kind": "clock", "advance": seconds, "now": clock.stamp(total), "why": "restart",
                  "inc": inc, "twin": 0, "seq": 0})


def _execute(scen: api.Scenario, spec: dict, workdir: Path, stack: contextlib.ExitStack,
             deadline: float | None, collect_stats: bool) -> Outcome:
    _time_left(deadline)
    if scen.net is not None:
        from ..net import _prepare, _seed

        _prepare()
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    if scen.net is not None:
        _seed(workdir, scen.net)
    copies = 2 if _delivered_twice(scen, spec, workdir) else 1
    completed, incarnations = False, 0
    policy = recovery.resolve(scen.recovery)
    ready = True
    try:
        values = fixtures.enter(stack, scen.fixtures, lambda made: api.Run(
            workdir, 0, spec.get("seed"), recovery.name_of(scen.recovery), made))
    except fixtures.FixtureUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken fixture is reported, not fatal
        _fixture_failed(workdir, exc)
        values, ready = {}, False
    spec = {**spec, "fixtures": values}
    if ready:
        ready = _run_setup(scen, spec, workdir)
    scheduled = fires = None
    with contextlib.closing(JsonlWriter(workdir / WORLD, fsync=False)) as parent:
        for inc in range(scen.max_restarts + 1 if ready else 0):
            incarnations += 1
            began = clock.offset(workdir)
            if scheduled is None:
                scheduled = began
            state = _incarnate(scen, {**spec, "fires": fires}, workdir, inc,
                               copies if inc == 0 else 1, parent, deadline)
            if state == "harness_error":
                break
            if state == "done":
                completed = True
                break
            resume = policy.resume(recovery.Died(inc, state, began, clock.offset(workdir),
                                                 scheduled))
            if resume is None:
                break
            if inc < scen.max_restarts:
                _pass_time(parent, workdir, inc + 1, resume.advance + scen.advance_on_restart)
                if not _run_actors(scen, spec, workdir, inc):
                    break
                fires = list(resume.fires) or None
                scheduled = resume.fires[-1] if resume.fires else clock.offset(workdir)
    entries = read_jsonl(workdir / WORLD)
    draws = _read_draws(workdir)
    world = World(entries, completed, incarnations, scen.max_restarts,
                  _run_for(scen, spec, workdir, max(incarnations - 1, 0)),
                  strict_scopes=scen.strict_scopes, delivery=api.declared_delivery(scen, entries))
    violations = []
    harness_errors = [e for e in entries if e["kind"] == "harness_error"]
    if harness_errors:
        first_err = harness_errors[0]
        violations.append(Violation("harness_error", first_err["text"], first_err.get("op")))
    else:
        for inv in [scenario_terminates(), *scen.invariants]:
            message = None
            strict_op = None
            if scen.strict_scopes and getattr(inv, "duplicate_sensitive", False):
                for watched in inv.ops:
                    if (err := check_strict_scope(world, watched)) is not None:
                        message = err
                        strict_op = watched
                        break
            if message is None:
                try:
                    message = inv.check(world)
                except Exception as exc:  # noqa: BLE001 - a broken invariant is a finding, not a crash
                    message = f"the invariant itself raised {describe(exc)}"
            if message:
                op = (strict_op or getattr(message, "op", None)
                      or (inv.ops[0] if len(inv.ops) == 1 else None))
                violations.append(Violation(inv.name, str(message), op, getattr(message, "scope", None)))
    stats = coverage.of_run(world, draws) if collect_stats else {}
    return Outcome(workdir, completed, entries + draws, {d["key"]: d["value"] for d in draws},
                   violations, stats)


def run_seed(scen: api.Scenario, seed: int, runs_dir: Path, *,
             deadline: float | None = None) -> Outcome:
    base = runs_dir / scen.name
    work = base / f".work-{os.getpid()}-{seed}"
    try:
        out = execute(scen, {"seed": seed}, work, deadline=deadline)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    shutil.rmtree(base / f"seed-{seed}.min", ignore_errors=True)
    (base / f"seed-{seed}.min.json").unlink(missing_ok=True)
    (base / f"seed-{seed}.nominal.min.json").unlink(missing_ok=True)
    if out.ok:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(base / f"seed-{seed}", ignore_errors=True)
        shutil.rmtree(base / "flaky" / f"seed-{seed}", ignore_errors=True)
    else:
        kept = base / f"seed-{seed}"
        shutil.rmtree(kept, ignore_errors=True)
        work.rename(kept)
        out.dir = kept
    return out


@dataclass
class FlakyDetail:
    seed: int
    dir: Path
    runs: int
    divergence: str | None = None

    def as_dict(self) -> dict:
        return {
            "seed": self.seed,
            "dir": str(self.dir),
            "runs": self.runs,
            "divergence": self.divergence,
        }


@dataclass
class Sweep:
    seeds: int
    failing: list[tuple[int, str, dict]]  # (seed, first violated invariant, realized), reproduced
    flaky: list[int]  # failed once, did not fail the same way when re-run
    cov: coverage.Coverage
    wall: float
    defect_keys: dict[int, DefectKey] = field(default_factory=dict)
    skipped_seeds: int = 0
    unconfirmed: list[int] = field(default_factory=list)
    flaky_details: dict[int, FlakyDetail] = field(default_factory=dict)

    def problems(self, scen: api.Scenario) -> list[str]:
        """Reasons a sweep that found no failure still proved nothing."""
        if self.skipped_seeds or self.unconfirmed:
            notes = []
            if self.skipped_seeds:
                notes.append(f"skipped {self.skipped_seeds} of "
                             f"{self.seeds + self.skipped_seeds} requested seeds")
            if self.unconfirmed:
                notes.append(f"skipped confirmation and minimization for {len(self.unconfirmed)} "
                             "seeds that failed on their first run")
            return ["INCOMPLETE: time budget exhausted, " + ", ".join(notes)]
        if self.failing or self.flaky:
            return []
        if not scen.expect_effects:
            return []
        if self.cov.harness_errors:
            sample = next(iter(self.cov.harness_errors))
            return [(f"NOTHING TESTED: harness hook raised across {self.seeds} seeds "
                     f"({sample}), so no effect could be checked.")]
        if not self.cov.calls:
            return [("NOTHING TESTED: no effect was called in any of "
                     f"{self.seeds} seeds (0 attempts), so every invariant passed vacuously. "
                     "Check that the agent reaches its tools (a model output that fails "
                     "validation never gets there). A scenario that must do nothing sets "
                     "expect_effects=False.")]
        if not self.cov.landed:
            no_eff = sum(self.cov.no_effects.values())
            if no_eff:
                reason = (f"{no_eff} returned without effect; check landed= or whether your tool "
                          "is refusing every call")
            else:
                reason = ("every attempt raised before completing; check whether the tool or "
                          "framework is failing every call")
            return [(f"NOTHING TESTED: {self.cov.calls} effect attempts ran across {self.seeds} "
                     f"seeds, but 0 landed ({reason}), so every invariant passed vacuously. "
                     "A scenario that must do nothing sets expect_effects=False.")]
        return []

    def notes(self, scen: api.Scenario) -> list[str]:
        ops = tuple(op for inv in scen.invariants for op in inv.ops)
        return [f"note: an invariant watches {op}, which no seed ever called"
                for op in coverage.never_ran(self.cov, ops)]


def _first_divergence(run_a: Path, run_b: Path, label_a: str, label_b: str,
                      inv_a: str, invs_b: list[str]) -> str:
    """Describe the first observable difference between two runs of the same seed."""
    story_a, _ = story(read_story(run_a))
    story_b, _ = story(read_story(run_b))
    for i in range(max(len(story_a), len(story_b))):
        la = story_a[i].strip() if i < len(story_a) else "<end of trace>"
        lb = story_b[i].strip() if i < len(story_b) else "<end of trace>"
        if la != lb:
            return (f"first divergence ({label_a} vs {label_b}): "
                    f"{label_a} had {la!r}, {label_b} had {lb!r}")
    draws_a = {d["key"]: d["value"] for d in _read_draws(run_a)}
    draws_b = {d["key"]: d["value"] for d in _read_draws(run_b)}
    for k in sorted(set(draws_a) | set(draws_b)):
        if draws_a.get(k) != draws_b.get(k):
            return (f"first divergence ({label_a} vs {label_b}): "
                    f"draw {k!r} was {draws_a.get(k)!r} in {label_a}, "
                    f"{draws_b.get(k)!r} in {label_b}")
    outs = sorted({p.name for p in run_a.glob("inc-*.out")}
                  | {p.name for p in run_b.glob("inc-*.out")})
    for name in outs:
        pa, pb = run_a / name, run_b / name
        ta = pa.read_text(errors="replace").splitlines() if pa.exists() else []
        tb = pb.read_text(errors="replace").splitlines() if pb.exists() else []
        for lineno in range(max(len(ta), len(tb))):
            la = ta[lineno] if lineno < len(ta) else "<eof>"
            lb = tb[lineno] if lineno < len(tb) else "<eof>"
            if la != lb:
                return (f"first divergence ({label_a} vs {label_b}, {name}:{lineno + 1}): "
                        f"{label_a} had {la!r}, {label_b} had {lb!r}")
    outcome_b = f"violated {', '.join(invs_b)}" if invs_b else "passed"
    return (f"first divergence ({label_a} vs {label_b}): identical traces, "
            f"but {label_a} violated {inv_a} while {label_b} {outcome_b}")


def _save_flaky_runs(base: Path, seed: int, temp_dirs: list[Path], diverged_at: int,
                     invariant: str, rerun_violations: list[list[str]]) -> dict:
    flaky_dir = base / "flaky" / f"seed-{seed}"
    shutil.rmtree(flaky_dir, ignore_errors=True)
    flaky_dir.mkdir(parents=True, exist_ok=True)
    initial = base / f"seed-{seed}"
    run0 = flaky_dir / "run-0"
    if initial.exists():
        shutil.copytree(initial, run0)
    saved = 0
    for k, work in enumerate(temp_dirs, 1):
        if work.exists() and (work / WORLD).exists():
            work.rename(flaky_dir / f"run-{k}")
            saved += 1
    divergence = _first_divergence(
        run0, flaky_dir / f"run-{diverged_at}",
        "run-0", f"run-{diverged_at}",
        invariant, rerun_violations[diverged_at - 1],
    )
    return {
        "seed": seed,
        "dir": str(flaky_dir),
        "runs": saved + 1,
        "divergence": divergence,
    }


def _confirm_seed(scen: api.Scenario, seed: int, invariant: str, runs_dir: Path,
                  deadline: float | None) -> tuple[bool, dict | None]:
    base = runs_dir / scen.name
    temp_dirs: list[Path] = []
    rerun_violations: list[list[str]] = []
    diverged_at: int | None = None
    try:
        for k in range(1, RERUNS + 1):
            work = base / f".rerun-{os.getpid()}-{seed}-{k}"
            temp_dirs.append(work)
            # A killed process leaves unbuffered writes in the kernel page cache.
            out = execute(scen, {"seed": seed, "sync_logs": False}, work, deadline=deadline,
                          collect_stats=False)
            rerun_violations.append([v.invariant for v in out.violations])
            if diverged_at is None and not out.violated(invariant):
                diverged_at = k
        if diverged_at is None:
            return True, None
        detail = _save_flaky_runs(base, seed, temp_dirs, diverged_at, invariant, rerun_violations)
        temp_dirs.clear()
        return False, detail
    except BudgetExpired:
        if diverged_at is not None:
            detail = _save_flaky_runs(base, seed, temp_dirs, diverged_at, invariant,
                                      rerun_violations)
            temp_dirs.clear()
            return False, detail
        raise
    finally:
        for work in temp_dirs:
            shutil.rmtree(work, ignore_errors=True)


def run_sweep(scen: api.Scenario, seeds: range, runs_dir: Path, jobs: int = 1, *,
              deadline: float | None = None) -> Sweep:
    """Run every seed, then re-run each failing seed: one that does not fail the same way
    again is flaky (it depends on something shape does not control), not a failure."""
    fixtures.require(scen.fixtures)
    started = time.perf_counter()

    def one(seed: int) -> list:
        out = run_seed(scen, seed, runs_dir, deadline=deadline)
        first = None if out.ok else out.violations[0]
        return [
            seed,
            None if first is None else first.invariant,
            None if first is None else first.op,
            None if first is None else first.scope,
            out.realized,
            out.stats,
        ]

    rows = _run_batch(seeds, jobs, one)
    cov = coverage.Coverage()
    failed = []
    defect_keys: dict[int, DefectKey] = {}
    for seed, invariant, op, scope, realized, stats in rows:
        cov.add(stats)
        if invariant is not None:
            failed.append((seed, invariant, realized))
            defect_keys[seed] = DefectKey(invariant, op, scope_pattern(scope))
    again = {seed: invariant for seed, invariant, _ in failed}

    def rerun(seed: int) -> list:
        ok, detail = _confirm_seed(scen, seed, again[seed], runs_dir, deadline)
        return [seed, ok, detail]

    rerun_rows = _run_batch(list(again), jobs, rerun)
    verdicts = {seed: ok for seed, ok, _ in rerun_rows}
    flaky_details = {
        seed: FlakyDetail(
            seed=detail["seed"], dir=Path(detail["dir"]),
            runs=detail["runs"], divergence=detail["divergence"],
        )
        for seed, ok, detail in rerun_rows
        if ok is False and detail is not None
    }
    failing = [f for f in failed if verdicts.get(f[0]) is True]
    flaky = [seed for seed in again if verdicts.get(seed) is False]
    unconfirmed = [seed for seed in again if seed not in verdicts]
    defect_keys = {seed: defect_keys[seed] for seed, _, _ in failing}
    if not failing and not flaky:
        with contextlib.suppress(OSError):
            (runs_dir / scen.name / "flaky").rmdir()
        with contextlib.suppress(OSError):
            (runs_dir / scen.name).rmdir()
    return Sweep(len(rows), failing, flaky, cov, time.perf_counter() - started, defect_keys,
                 len(seeds) - len(rows), unconfirmed, flaky_details)


def sweep(scen: api.Scenario, seeds: range, runs_dir: Path,
          jobs: int = 1) -> list[tuple[int, str, dict]]:
    """The reproduced failures of `run_sweep`: (seed, first violated invariant, realized)."""
    return run_sweep(scen, seeds, runs_dir, jobs).failing


def _until_budget(seeds, one) -> list:
    rows = []
    for seed in seeds:
        try:
            rows.append(one(seed))
        except BudgetExpired:
            break
    return rows


def _run_batch(seeds, jobs: int, one) -> list:
    jobs = min(jobs, len(seeds))
    if jobs <= 1 or not hasattr(os, "fork"):
        return _until_budget(seeds, one)
    workers = []
    sys.stdout.flush()  # a forked worker must not inherit, and later flush, the parent's output
    sys.stderr.flush()
    for chunk in (seeds[i::jobs] for i in range(jobs)):
        read, write = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read)
            code = 1
            try:
                payload = json.dumps(_until_budget(chunk, one))
                with os.fdopen(write, "w") as f:
                    f.write(payload)
                code = 0
            finally:
                os._exit(code)
        os.close(write)
        workers.append((pid, read))
    results: list = []
    for pid, read in workers:
        with os.fdopen(read) as f:
            text = f.read()
        _, status = os.waitpid(pid, 0)
        if os.waitstatus_to_exitcode(status) != 0:
            raise RuntimeError("a shape worker died before reporting its seeds")
        results.extend(json.loads(text))
    return sorted(results)


# --------------------------------------------------------------------------------------- reporting


def story(entries: list[dict]) -> tuple[list[str], int]:
    """The trace in the code's words, and its step count (calls, approvals and deaths)."""
    events = [e for e in entries
              if e["kind"] in ("effect", "no_effect", "approval", "claim", "choice", "error",
                               "harness_error", "incarnation", "note", "point", "schedule", "clock")
              or (e["kind"] == "fault" and e["fault"] not in api.AFTER_COMMIT)]
    events.sort(key=event_order)
    copies = any(e.get("twin") for e in events)  # the trigger was delivered twice
    lines: list[str] = []
    steps, current = 0, None
    for e in events:
        group = (e["inc"], e.get("twin", 0), e.get("actor_id"))
        if group != current:
            current = group
            label = ("setup (no faults)" if group[0] < 0
                     else f"incarnation {group[0]}" + (" (restarted)" if group[0] else ""))
            if copies and group[0] == 0 and group[2] in (None, 0, 1):
                label += f" (copy {'AB'[group[1]]} of the duplicate delivery)"
            if group[2] is not None:
                label += f", actor {group[2]} {e['actor_name']}"
            lines.append(label)
        kind = e["kind"]
        if kind == "clock":
            lines.append(f"  time passed: {e['advance']:g}s (clock {e['now']})")
        elif e.get("setup"):
            if kind in ("effect", "no_effect"):
                lines.append(f"  {e['call']}" + (" returned without effect" if kind == "no_effect"
                                                  else ""))
        elif kind in ("effect", "fault"):
            steps += 1
            fault = e["fault"]
            note = FAULT_TEXT[fault].format(exc=e["raised"]) if fault else ""
            if kind == "effect":
                landed = "effect landed" + (f" (idempotency key {e['key']!r})" if e["key"] else "")
                note = f"{landed}, then {note}" if fault else landed
            lines += [f"  {steps}. {e['call']}", f"       {note}"]
        elif kind == "no_effect":
            steps += 1
            lines += [f"  {steps}. {e['call']}", "       returned without effect"]
        elif kind == "choice":
            steps += 1
            by = f" [world actor {e['actor']}]" if e.get("actor") else ""
            lines.append(f"  {steps}. model chose {e['chosen']} at {e['choice']!r}"
                         f" (nominal: {e['nominal']}){by}")
        elif kind == "schedule":
            steps += 1
            who = "scheduler" if e["schedule"] == "actors" else "asyncio"
            lines.append(f"  {steps}. {who} ran {e['chosen']} before {e['nominal']}")
        elif kind == "point":
            steps += 1
            lines.append(f"  {steps}. at {e['label']!r}")
        elif kind == "approval":
            steps += 1
            lines.append(f"  {steps}. approval recorded for {e['scope']!r}")
        elif kind == "claim":
            steps += 1
            verdict = "happened" if e.get("ok", True) else "did not happen"
            lines.append(f"  {steps}. agent told the user {e['op']} {verdict} for {e['scope']!r}")
        elif kind == "note":
            lines.append(f"  {e['text']}")
        elif kind == "harness_error":
            lines.append(f"  harness error: {e['text']}")
        elif kind == "error":
            lines.append(f"  process raised: {e['text']}")
        elif e["how"] == "done":
            lines.append("  finished")
        elif e["how"] == "timeout":
            lines.append(f"  killed after {INCARNATION_TIMEOUT:.0f}s without finishing")
        elif e["how"] == "crash":
            lines.append("  -> process died")
    return lines, steps


def _consumed(spec_keys: dict[str, int], out: Outcome) -> bool:
    return all(k in out.realized for k in spec_keys)


def _failure_signature(outcome: Outcome, violation: Violation) -> tuple:
    """Fingerprint how a defect failed, to tell nominal failures apart from fault-driven ones."""
    if violation.invariant.startswith(("approved_before(", "requires(", "within_scopes(")):
        return (violation.message,)
    op = violation.op
    raw_effects = [
        e for e in outcome.entries
        if e["kind"] == "effect" and (op is None or e["op"] == op)
    ]
    if raw_effects:
        base_inc = raw_effects[0]["inc"]
        effects = tuple(
            (e["inc"] - base_inc, e["op"], e["scope"], e["call"])
            for e in raw_effects
        )
        attempts = ()
    else:
        raw_attempts = [
            e for e in outcome.entries
            if e["kind"] == "attempt" and (op is None or e["op"] == op)
        ]
        base_inc = raw_attempts[0]["inc"] if raw_attempts else 0
        effects = ()
        attempts = tuple(
            (e["inc"] - base_inc, e["op"], e["scope"], e["call"])
            for e in raw_attempts
        )
    incarnations = () if outcome.completed else tuple(
        (e["inc"], e["how"])
        for e in outcome.entries
        if e["kind"] == "incarnation"
    )
    return (violation.message, effects, attempts, incarnations)


def _raw_triggers(realized: dict[str, int]) -> dict[str, set[str]]:
    """Derive trigger classes directly from a realized map before shrinking."""
    classes: dict[str, set[str]] = {}
    for key, value in realized.items():
        if key.startswith(("id:", "jitter:")):
            continue
        if key == DUPLICATE_KEY:
            classes.setdefault("duplicate_trigger", set())
        elif key.startswith("model:"):
            classes.setdefault("model", set())
        elif key.startswith("sched:"):
            classes.setdefault("interleaving", set())
        elif key.startswith("fault:") and 1 <= value <= len(FAULTS):
            fault = FAULTS[value - 1]
            classes.setdefault(fault_class(fault), set()).add(fault)
    return classes or {"nominal": set()}


def _rep_seeds(defect: Defect) -> set[int]:
    seeds = {t.failure.seed for t in defect.triggers.values()}
    if defect.failure is not None:
        seeds.add(defect.failure.seed)
    return seeds


def _write_replay_json(path: Path, scen: api.Scenario, found: DefectKey, seed: int,
                       realized: dict[str, int], *, reproduces: bool = True) -> None:
    path.write_text(json.dumps({
        "scenario": scen.name, "file": scen.file, **dataclasses.asdict(found), "seed": seed,
        "realized": realized, "reproduces": reproduces,
    }, indent=2))


def _shrink_confirmed(
    scen: api.Scenario,
    seed: int,
    invariant: str,
    realized: dict[str, int],
    runs_dir: Path,
    target: str,
    key: DefectKey,
    first: Outcome,
    first_v: Violation,
    *,
    deadline: float | None = None,
    avoid_signature: tuple | None = None,
    replay_path: Path | None = None,
    final_dir: Path | None = None,
) -> tuple[Failure, Outcome, Violation | None, dict[str, int]]:
    """Shrink an already-confirmed failing seed and verify the minimal replay."""
    base = runs_dir / scen.name
    kept = base / f"seed-{seed}"
    work = base / f".shrink-{os.getpid()}"
    out_dir = final_dir if final_dir is not None else base / f"seed-{seed}.min"
    path = replay_path if replay_path is not None else base / f"seed-{seed}.min.json"
    replays = 1  # `first` was the confirmation replay

    def matching(outcome: Outcome) -> Violation | None:
        return next((v for v in outcome.violations if v.invariant == invariant
                     and DefectKey.from_violation(v) == key), None)

    def fails(candidate: dict[str, int]) -> bool:
        nonlocal replays
        replays += 1
        outcome = execute(scen, {"explicit": candidate, "seed": seed, "sync_logs": False},
                          work, deadline=deadline, collect_stats=False)
        v = matching(outcome)
        if v is None or not _consumed(candidate, outcome):
            return False
        return avoid_signature is None or _failure_signature(outcome, v) != avoid_signature

    def build_failure(entries: list[dict], message: str, small: dict[str, int],
                      problem: str | None = None) -> Failure:
        trace, steps = story(entries)
        return Failure(
            target, seed, key, message, trace, steps, len(small), len(realized), replays, path,
            problem,
            trigger_classes(entries) if problem is None else {},
            describe_triggers(entries) if problem is None else "",
        )

    try:
        small = shrink(realized, fails) if realized else realized
        final = first
        if small != realized or first.dir != out_dir:
            final = execute(scen, {"explicit": small, "seed": seed}, out_dir, deadline=deadline,
                            collect_stats=False)
            replays += 1
        final_v = matching(final)
        if final_v is None or not _consumed(small, final):
            shutil.rmtree(out_dir, ignore_errors=True)
            return (
                build_failure(
                    read_story(kept), first_v.message, realized,
                    "the minimized run diverged on replay, so the minimal trace is not trusted",
                ),
                final,
                None,
                realized,
            )
        return build_failure(final.entries, final_v.message, small), final, final_v, small
    except BudgetExpired:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def investigate(scen: api.Scenario, seed: int, invariant: str, realized: dict[str, int],
                runs_dir: Path, target: str, *, key: DefectKey | None = None,
                deadline: float | None = None) -> Failure:
    """Confirm a failing seed by replay, shrink it, and describe the minimal run."""
    base = runs_dir / scen.name
    kept = base / f"seed-{seed}"
    final_dir = base / f"seed-{seed}.min"
    path = base / f"seed-{seed}.min.json"

    def matching(outcome: Outcome) -> Violation | None:
        return next((v for v in outcome.violations if v.invariant == invariant
                     and (key is None or DefectKey.from_violation(v) == key)), None)

    try:
        first = execute(scen, {"explicit": realized, "seed": seed}, final_dir, deadline=deadline,
                        collect_stats=False)
        first_v = matching(first)
        if first_v is None or not _consumed(realized, first):
            why = ("replaying its recorded draws " + (
                "did not reproduce the defect" if first_v is None
                else "reached only some of the recorded keys"
            ) + ": the run depends on something shape does not control (time, threads,"
                " an unscripted model), so it cannot be replayed or minimized")
            shutil.rmtree(final_dir, ignore_errors=True)
            found_key = key or DefectKey(invariant, None, None)
            _write_replay_json(path, scen, found_key, seed, realized, reproduces=False)
            trace, steps = story(read_story(kept))
            message = first_v.message if first_v else "(did not reproduce)"
            return Failure(target, seed, found_key, message, trace, steps, len(realized),
                           len(realized), 1, path, why, {})

        found_key = DefectKey.from_violation(first_v)
        failure, _, _, small = _shrink_confirmed(
            scen, seed, invariant, realized, runs_dir, target, found_key,
            first, first_v, deadline=deadline, replay_path=path, final_dir=final_dir,
        )
        _write_replay_json(path, scen, found_key, seed, small, reproduces=failure.problem is None)
        return failure
    except BudgetExpired:
        shutil.rmtree(final_dir, ignore_errors=True)
        raise


def investigate_all(scen: api.Scenario, failing: list[tuple[int, str, dict]], runs_dir: Path,
                    target: str, limit: int = MAX_INVESTIGATED, *,
                    defect_keys: dict[int, DefectKey] | None = None,
                    deadline: float | None = None) -> Report:
    """Group failing seeds by defect, minimize one representative per (defect, trigger class),
    and confirm remaining seeds by replay without writing extra `.min.json` files."""
    ordered = sorted(failing, key=lambda f: (len(f[2]), f[0]))
    defects: dict[DefectKey, Defect] = {}
    nominal_sigs: dict[DefectKey, tuple] = {}
    attempted_combos: dict[DefectKey, set[frozenset[str]]] = {}
    report = Report(len(failing), [])
    base = runs_dir / scen.name
    work = base / f".confirm-{os.getpid()}"
    investigated = 0

    def needs_shrink(d_key: DefectKey, trig: dict[str, set[str]]) -> bool:
        d = defects.get(d_key)
        if d is None or not d.triggers:
            return True
        if frozenset(trig) in attempted_combos.get(d_key, ()):
            return False
        if set(trig) - set(d.triggers):
            return True
        return any(
            len(trig) < len(d.triggers[c].failure.triggers)
            for c in trig
            if c in d.triggers
        )

    def record_rep(defect: Defect, found: Failure, small: dict[str, int], seed: int) -> None:
        is_rep = (
            defect.failure is None
            or found.size < defect.failure.size
            or any(
                name not in defect.triggers or found.size < defect.triggers[name].failure.size
                for name in found.triggers
            )
        )
        if is_rep:
            prev_reps = _rep_seeds(defect)
            _write_replay_json(found.replay_file, scen, defect.key, seed, small, reproduces=True)
            defect.record(found)
            new_reps = _rep_seeds(defect)
            for old_seed in prev_reps - new_reps:
                (base / f"seed-{old_seed}.min.json").unlink(missing_ok=True)
                shutil.rmtree(base / f"seed-{old_seed}.min", ignore_errors=True)
        else:
            shutil.rmtree(base / f"seed-{seed}.min", ignore_errors=True)
            defect.record_confirmed(seed, found.triggers)

    try:
        for seed, invariant, realized in ordered:
            key = defect_keys.get(seed) if defect_keys is not None else None
            if limit == 0:
                report.skipped_seeds.append(seed)
                if key is not None:
                    defects.setdefault(key, Defect(key)).seeds.append(seed)
                continue

            raw_from_map = _raw_triggers(realized)
            might_shrink = key is None or needs_shrink(key, raw_from_map)
            if might_shrink and investigated >= limit:
                report.skipped_seeds.append(seed)
                if key is not None:
                    defects.setdefault(key, Defect(key)).seeds.append(seed)
                continue

            confirm_dir = base / f"seed-{seed}.min" if might_shrink else work
            try:
                first = execute(
                    scen,
                    {"explicit": realized, "seed": seed, "sync_logs": might_shrink},
                    confirm_dir,
                    deadline=deadline,
                    collect_stats=False,
                )
            except BudgetExpired:
                if might_shrink:
                    shutil.rmtree(confirm_dir, ignore_errors=True)
                report.budget_skipped_seeds.append(seed)
                if key is not None:
                    defects.setdefault(key, Defect(key)).seeds.append(seed)
                continue

            first_v = next(
                (v for v in first.violations if v.invariant == invariant
                 and (key is None or DefectKey.from_violation(v) == key)),
                None,
            )
            if first_v is None or not _consumed(realized, first):
                if might_shrink:
                    shutil.rmtree(confirm_dir, ignore_errors=True)
                why = ("replaying its recorded draws " + (
                    "did not reproduce the defect" if first_v is None
                    else "reached only some of the recorded keys"
                ) + ": the run depends on something shape does not control (time, threads,"
                    " an unscripted model), so it cannot be replayed or minimized")
                found_key = key or DefectKey(invariant, None, None)
                trace, steps = story(read_story(base / f"seed-{seed}"))
                message = first_v.message if first_v else "(did not reproduce)"
                unrep = Failure(target, seed, found_key, message, trace, steps, len(realized),
                                len(realized), 1, base / f"seed-{seed}.min.json", why, {})
                defect = defects.setdefault(found_key, Defect(found_key))
                defect.record(unrep)
                defect.seeds.append(seed)
                report.unreproducible.append(unrep)
                continue

            key = DefectKey.from_violation(first_v)
            defect = defects.setdefault(key, Defect(key))
            raw_triggers = trigger_classes(first.entries)
            sig = _failure_signature(first, first_v)

            nom_sig = nominal_sigs.get(key)
            if nom_sig is not None and sig == nom_sig:
                if might_shrink:
                    shutil.rmtree(confirm_dir, ignore_errors=True)
                defect.record_confirmed(seed, {"nominal": set()})
                defect.seeds.append(seed)
                continue

            should_shrink = needs_shrink(key, raw_triggers)
            if should_shrink and investigated >= limit:
                if might_shrink:
                    shutil.rmtree(confirm_dir, ignore_errors=True)
                report.skipped_seeds.append(seed)
                defect.seeds.append(seed)
                continue

            if not should_shrink:
                if might_shrink:
                    shutil.rmtree(confirm_dir, ignore_errors=True)
                defect.record_confirmed(seed, raw_triggers)
                defect.seeds.append(seed)
                continue

            attempted_combos.setdefault(key, set()).add(frozenset(raw_triggers))
            avoid = nom_sig if (nom_sig is not None and sig != nom_sig) else None
            try:
                found, final_out, final_v, small = _shrink_confirmed(
                    scen, seed, invariant, realized, runs_dir, target, key,
                    first, first_v, deadline=deadline, avoid_signature=avoid,
                )
            except BudgetExpired:
                report.budget_skipped_seeds.append(seed)
                defect.seeds.append(seed)
                continue

            investigated += 1
            if found.problem:
                defect.record(found)
                defect.seeds.append(seed)
                report.unreproducible.append(found)
                continue

            if found.deviations == 0 and key not in nominal_sigs and final_v is not None:
                nominal_sigs[key] = _failure_signature(final_out, final_v)
                if realized and sig != nominal_sigs[key]:
                    # This fault-driven seed shrank to nominal, revealing both a nominal bug and
                    # a distinct fault-driven failure; preserve the nominal replay separately and
                    # re-shrink while avoiding the nominal signature.
                    nom_path = base / f"seed-{seed}.nominal.min.json"
                    found.replay_file = nom_path
                    record_rep(defect, found, small, seed)
                    try:
                        fault_found, _, _, fault_small = _shrink_confirmed(
                            scen, seed, invariant, realized, runs_dir, target, key,
                            first, first_v, deadline=deadline, avoid_signature=nominal_sigs[key],
                        )
                    except BudgetExpired:
                        report.budget_skipped_seeds.append(seed)
                    else:
                        if not fault_found.problem:
                            record_rep(defect, fault_found, fault_small, seed)
                    defect.seeds.append(seed)
                    continue

            record_rep(defect, found, small, seed)
            defect.seeds.append(seed)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    report.defects = sorted(defects.values(), key=lambda d: (
        list(d.triggers) == ["nominal"], -len(d.seeds), min(d.seeds)
    ))
    return report


def _read_draws(workdir: Path) -> list[dict]:
    return [d | {"kind": ("choice" if "choice" in d else
                         "schedule" if "schedule" in d else "fault")}
            for d in read_jsonl(workdir / REALIZED)]


def read_story(workdir: Path) -> list[dict]:
    return read_jsonl(workdir / WORLD) + _read_draws(workdir)


def replay_file(scen: api.Scenario, path: Path, runs_dir: Path) -> tuple[int, str]:
    """Replay a saved failure. Returns (exit code, text)."""
    data = json.loads(path.read_text())
    fixtures.require(scen.fixtures)
    seed = data.get("seed") if "scope_pattern" in data else None
    out = execute(scen, {"explicit": data["realized"], "seed": seed},
                  runs_dir / scen.name / "replay")
    invariant = data["invariant"]
    trace, _ = story(out.entries)
    body = "\n".join(f"  {line}" for line in trace)
    expected = DefectKey(invariant, data.get("op"), data.get("scope_pattern"))
    match = next((v for v in out.violations if v.invariant == invariant
                  and ("scope_pattern" not in data or DefectKey.from_violation(v) == expected)), None)
    if match is not None:
        message = match.message
        diverged = not _consumed(data["realized"], out)
        note = "\n  note: the run diverged from the file" if diverged else ""
        return 1, f"FAIL {scen.name}  invariant={invariant} (replayed)\n  {message}\n{body}{note}"
    if not _consumed(data["realized"], out):
        return 2, f"DIVERGED {scen.name}: the replay did not reach the recorded deviations\n{body}"
    return 0, f"PASS {scen.name}: the recorded run no longer violates {invariant}\n{body}"


def render_flaky(flaky: list[int], details: dict[int, FlakyDetail] | None = None) -> str:
    shown = ", ".join(map(str, flaky[:10])) + (", ..." if len(flaky) > 10 else "")
    lines = [(
        f"FLAKY: {len(flaky)} seeds failed once but did not fail the same way when re-run "
        f"{RERUNS} more times ({shown}). They depend on something shape does not control "
        "(threads, timers, an unscripted model), so they are not counted as failures and "
        "were not minimized; their runs and confirmation reruns are kept under the runs dir."
    )]
    if details:
        for seed in flaky[:10]:
            info = details.get(seed)
            if info is None:
                continue
            last = max(info.runs - 1, 0)
            lines.append(f"  seed {seed}: {info.dir} (run-0..run-{last})")
            if info.divergence:
                lines.append(f"    {info.divergence}")
    return "\n".join(lines)


def replay(scen: api.Scenario, path: str | Path, runs_dir: str | Path = "runs") -> None:
    """Replay a saved `.min.json` regression trace. Returns None if the recorded invariant no
    longer fails and all recorded deviations were reached; raises AssertionError otherwise."""
    code, text = replay_file(scen, Path(path), Path(runs_dir))
    if code != 0:
        raise AssertionError(text)


def check(scen: api.Scenario, seeds: int = 100, start: int = 0,
          runs_dir: str | Path = "runs", jobs: int = 1, *, budget: float | None = None,
          max_shrinks: int = MAX_INVESTIGATED, strict_scopes: bool | None = None) -> None:
    """Run `seeds` seeds (in `jobs` forked workers where available). Raises AssertionError
    carrying the minimized trace on failure. A time budget includes confirmation and shrinking.
    `max_shrinks` limits failed seeds investigated and can be zero."""
    if max_shrinks < 0:
        raise ValueError("max_shrinks must be non-negative")
    if strict_scopes is not None:
        scen = dataclasses.replace(scen, strict_scopes=strict_scopes)
    deadline = deadline_for(budget)
    runs = Path(runs_dir)
    swept = run_sweep(scen, range(start, start + seeds), runs, jobs=jobs, deadline=deadline)
    problems = swept.problems(scen)
    if not (swept.failing or swept.flaky or problems):
        return
    parts = [f"{len(swept.failing)} of {swept.seeds} seeds failed"
             + (f", {len(swept.flaky)} flaky" if swept.flaky else ""), swept.cov.render()]
    if scopes_text := swept.cov.render_scopes(scen.invariants):
        parts.append(scopes_text)
    if swept.failing:
        parts.append(
            investigate_all(scen, swept.failing, runs, scen.name, limit=max_shrinks,
                            defect_keys=swept.defect_keys, deadline=deadline).render()
        )
    if swept.flaky:
        parts.append(render_flaky(swept.flaky, swept.flaky_details))
    raise AssertionError("\n\n".join(parts + problems))
