"""Runs a seed as a chain of incarnations (one forked process each), evaluates the invariants over
the world log, shrinks a failure by replaying realized draws through `ExplicitChoices`, and
renders it in the code's own words."""

from __future__ import annotations

import json
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
from . import api, coverage
from .failure import Cause, Failure, Report, kinds, raw_kinds
from .faults import CRASH_EXIT, DUPLICATE_KEY, DUPLICATE_TRIGGER, FAULT_TEXT
from .invariants import Violation, World, scenario_terminates
from .log import PARENT_SEQ, REALIZED, WORLD, JsonlWriter, read_jsonl
from .rootcause import describe

INCARNATION_TIMEOUT = 30.0  # seconds; a hung incarnation counts as one that never finished
TIMEOUT_EXIT = 124
RERUNS = 2  # a failing seed must fail the same way this many more times to count as a failure
MAX_INVESTIGATED = 200  # failing seeds minimized per sweep, fewest deviations first


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
    return SeededChoices(spec["seed"]) if "seed" in spec else ExplicitChoices(spec["explicit"])


# ---------------------------------------------------------------------------------- incarnations


def child_main(scen: api.Scenario, spec: dict, workdir: Path, inc: int, twin: int = 0) -> int:
    """Run one incarnation inside the current (already isolated) process. Returns its exit code.
    `twin` is 1 for the second copy of a duplicated incarnation 0."""
    out = os.open(workdir / f"inc-{inc}{f'-t{twin}' if twin else ''}.out", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.dup2(out, 1)
    os.dup2(out, 2)
    sys.stdout = sys.stderr = os.fdopen(1, "w", buffering=1, closefd=False)
    # Fairness: faults eventually stop. With restarts allowed, the last incarnation runs fault-free,
    # so scenario_terminates asks whether the agent recovers, not whether the harness can outlast it.
    quiet = scen.max_restarts > 0 and inc == scen.max_restarts
    ctx = api._Context(workdir, inc, make_choices(spec), quiet, twin)
    api.activate(ctx)
    watchdog = threading.Timer(INCARNATION_TIMEOUT, lambda: os._exit(TIMEOUT_EXIT))
    watchdog.daemon = True
    watchdog.start()
    try:
        scen.fn(api.Run(workdir, inc, spec.get("seed")))
        return 0
    except BaseException as exc:  # noqa: BLE001 - anything that kills the agent is a death to log
        ctx.append(ctx.world, {"kind": "error", "text": describe(exc)})
        sys.stderr.write(traceback.format_exc())
        return 1


def _classify(code: int) -> str:
    return {0: "done", CRASH_EXIT: "crash", TIMEOUT_EXIT: "timeout"}.get(code, "error")


def _fork_incarnations(scen: api.Scenario, spec: dict, workdir: Path, inc: int,
                       copies: int) -> list[int]:
    sys.stdout.flush()
    sys.stderr.flush()
    pids = []
    for twin in range(copies):
        pid = os.fork()
        if pid == 0:
            code = 70
            try:
                code = child_main(scen, spec, workdir, inc, twin)
            finally:
                os._exit(code)
        pids.append(pid)
    codes = []
    for pid in pids:
        _, status = os.waitpid(pid, 0)
        codes.append(os.waitstatus_to_exitcode(status) if not os.WIFSIGNALED(status) else 1)
    return codes


def _spawn_incarnations(scen: api.Scenario, spec: dict, workdir: Path, inc: int,
                        copies: int) -> list[int]:
    root = str(Path(__file__).resolve().parents[2])
    paths = [root, *filter(None, [os.environ.get("PYTHONPATH")])]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(paths)}
    procs = []
    for twin in range(copies):
        payload = json.dumps({"file": scen.file, "name": scen.name, "spec": spec,
                              "workdir": str(workdir), "inc": inc, "twin": twin})
        procs.append(subprocess.Popen([sys.executable, "-m", "shape.boundary.child", payload],
                                      env=env))
    return [code if code >= 0 else 1 for code in (proc.wait() for proc in procs)]


def _incarnate(scen: api.Scenario, spec: dict, workdir: Path, inc: int, copies: int) -> list[int]:
    use_fork = hasattr(os, "fork") and os.environ.get("SHAPE_START_METHOD") != "spawn"
    return (_fork_incarnations if use_fork else _spawn_incarnations)(scen, spec, workdir, inc,
                                                                      copies)


def _delivered_twice(scen: api.Scenario, spec: dict, workdir: Path) -> bool:
    """Draw whether this seed delivers the trigger twice: incarnation 0 starts as two copies."""
    if not scen.duplicate_trigger:
        return False
    choices = make_choices(spec)
    weights = [1.0, 1.0 if choices.enabled(DUPLICATE_TRIGGER) else 0.0]
    if not choices.draw(DUPLICATE_KEY, weights):
        return False
    log = JsonlWriter(workdir / REALIZED)
    log.write({"key": DUPLICATE_KEY, "value": 1, "fault": DUPLICATE_TRIGGER,
               "op": coverage.SCENARIO_OP, "call": "the scenario is triggered", "raised": None,
               "inc": 0, "twin": 0, "seq": 0})
    log.close()
    return True


def _run_actors(scen: api.Scenario, spec: dict, workdir: Path, inc: int) -> None:
    """Call the world actors in the parent, between incarnation `inc` and the next one."""
    if not scen.actors:
        return
    ctx = api._Context(workdir, inc, make_choices(spec), actors=True)
    api.activate(ctx)
    try:
        for act in scen.actors:
            ctx.actor = act.name
            try:
                act(api.Run(workdir, inc + 1, spec.get("seed")))
            except Exception as exc:  # noqa: BLE001 - a broken actor is reported, not fatal
                ctx.append(ctx.world, {"kind": "error",
                                       "text": f"world actor {act.name} raised {describe(exc)}"})
    finally:
        api.activate(None)
        ctx.close()


# ------------------------------------------------------------------------------------------ a seed


def execute(scen: api.Scenario, spec: dict, workdir: Path) -> Outcome:
    """Run one seed (or one explicit replay) in `workdir`, restarting after every death."""
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    parent = JsonlWriter(workdir / WORLD, fsync=False)
    copies = 2 if _delivered_twice(scen, spec, workdir) else 1
    completed, incarnations = False, 0
    for inc in range(scen.max_restarts + 1):
        incarnations += 1
        hows = [_classify(code) for code in _incarnate(scen, spec, workdir, inc,
                                                       copies if inc == 0 else 1)]
        for twin, how in enumerate(hows):
            parent.write({"kind": "incarnation", "inc": inc, "twin": twin, "seq": PARENT_SEQ,
                          "how": how})
        if "done" in hows:  # a delivery somebody finished counts as handled
            completed = True
            break
        if inc < scen.max_restarts:
            _run_actors(scen, spec, workdir, inc)
    parent.close()
    entries = read_jsonl(workdir / WORLD)
    draws = [d | {"kind": "choice" if "choice" in d else "fault"}
             for d in read_jsonl(workdir / REALIZED)]
    world = World(entries, completed, incarnations, scen.max_restarts,
                  api.Run(workdir, incarnations - 1, spec.get("seed")))
    violations = []
    for inv in [scenario_terminates(), *scen.invariants]:
        try:
            message = inv.check(world)
        except Exception as exc:  # noqa: BLE001 - a broken invariant is a finding, not a crash
            message = f"the invariant itself raised {describe(exc)}"
        if message:
            violations.append(Violation(inv.name, message))
    return Outcome(workdir, completed, entries + draws, {d["key"]: d["value"] for d in draws},
                   violations, coverage.of_run(world, draws))


def run_seed(scen: api.Scenario, seed: int, runs_dir: Path) -> Outcome:
    base = runs_dir / scen.name
    work = base / f".work-{os.getpid()}-{seed}"
    out = execute(scen, {"seed": seed}, work)
    if out.ok:
        shutil.rmtree(work)
    else:
        kept = base / f"seed-{seed}"
        shutil.rmtree(kept, ignore_errors=True)
        work.rename(kept)
        out.dir = kept
    return out


@dataclass
class Sweep:
    seeds: int
    failing: list[tuple[int, str, dict]]  # (seed, first violated invariant, realized), reproduced
    flaky: list[int]  # failed once, did not fail the same way when re-run
    cov: coverage.Coverage
    wall: float

    def problems(self, scen: api.Scenario) -> list[str]:
        """Reasons a sweep that found no failure still proved nothing."""
        if self.failing or self.flaky:
            return []
        if scen.expect_effects and not self.cov.calls:
            return [("NOTHING TESTED: no @shape.effect function was called in any of "
                     f"{self.seeds} seeds, so every invariant passed vacuously. Check that the "
                     "agent reaches its tools (a model output that fails validation never gets "
                     "there). A scenario that must do nothing sets expect_effects=False.")]
        return []

    def notes(self, scen: api.Scenario) -> list[str]:
        ops = tuple(op for inv in scen.invariants for op in inv.ops)
        return [f"note: an invariant watches {op}, which no seed ever called"
                for op in coverage.never_ran(self.cov, ops)]


def _reproduces(scen: api.Scenario, seed: int, invariant: str, runs_dir: Path) -> bool:
    work = runs_dir / scen.name / f".rerun-{os.getpid()}-{seed}"
    try:
        return all(execute(scen, {"seed": seed}, work).violated(invariant) for _ in range(RERUNS))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_sweep(scen: api.Scenario, seeds: range, runs_dir: Path, jobs: int = 1) -> Sweep:
    """Run every seed, then re-run each failing seed: one that does not fail the same way
    again is flaky (it depends on something shape does not control), not a failure."""
    started = time.perf_counter()

    def one(seed: int) -> list:
        out = run_seed(scen, seed, runs_dir)
        return [seed, None if out.ok else out.violations[0].invariant, out.realized, out.stats]

    if jobs <= 1 or not hasattr(os, "fork"):
        rows = [one(s) for s in seeds]
    else:
        rows = _parallel(list(seeds), jobs, one)
    cov = coverage.Coverage()
    failed = []
    for seed, invariant, realized, stats in rows:
        cov.add(stats)
        if invariant is not None:
            failed.append((seed, invariant, realized))
    again = {seed: invariant for seed, invariant, _ in failed}

    def rerun(seed: int) -> list:
        return [seed, _reproduces(scen, seed, again[seed], runs_dir)]

    seeds_failed = [f[0] for f in failed]
    if jobs <= 1 or not hasattr(os, "fork") or len(seeds_failed) < 2:
        verdicts = dict(rerun(s) for s in seeds_failed)
    else:
        verdicts = dict(_parallel(seeds_failed, jobs, rerun))
    failing = [f for f in failed if verdicts[f[0]]]
    flaky = [f[0] for f in failed if not verdicts[f[0]]]
    return Sweep(len(seeds), failing, flaky, cov, time.perf_counter() - started)


def sweep(scen: api.Scenario, seeds: range, runs_dir: Path,
          jobs: int = 1) -> list[tuple[int, str, dict]]:
    """The reproduced failures of `run_sweep`: (seed, first violated invariant, realized)."""
    return run_sweep(scen, seeds, runs_dir, jobs).failing


def _parallel(seeds: list[int], jobs: int, one) -> list:
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
                payload = json.dumps([one(s) for s in chunk])
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
              if e["kind"] in ("effect", "approval", "claim", "choice", "error", "incarnation")
              or (e["kind"] == "fault" and e["fault"] not in api.AFTER_COMMIT)]
    events.sort(key=lambda e: (e["inc"], e.get("twin", 0), e["seq"]))
    copies = any(e.get("twin") for e in events)  # the trigger was delivered twice
    lines: list[str] = []
    steps, current = 0, None
    for e in events:
        group = (e["inc"], e.get("twin", 0))
        if group != current:
            current = group
            label = f"incarnation {group[0]}" + (" (restarted)" if group[0] else "")
            lines.append(label + (f" (copy {'AB'[group[1]]} of the duplicate delivery)"
                                  if copies and group[0] == 0 else ""))
        kind = e["kind"]
        if kind in ("effect", "fault"):
            steps += 1
            fault = e["fault"]
            note = FAULT_TEXT[fault].format(exc=e["raised"]) if fault else ""
            if kind == "effect":
                landed = "effect landed" + (f" (idempotency key {e['key']!r})" if e["key"] else "")
                note = f"{landed}, then {note}" if fault else landed
            lines += [f"  {steps}. {e['call']}", f"       {note}"]
        elif kind == "choice":
            steps += 1
            by = f" [world actor {e['actor']}]" if e.get("actor") else ""
            lines.append(f"  {steps}. model chose {e['chosen']} at {e['choice']!r}"
                         f" (nominal: {e['nominal']}){by}")
        elif kind == "approval":
            steps += 1
            lines.append(f"  {steps}. approval recorded for {e['scope']!r}")
        elif kind == "claim":
            steps += 1
            lines.append(f"  {steps}. agent told the user {e['op']} happened for {e['scope']!r}")
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


def investigate(scen: api.Scenario, seed: int, invariant: str, realized: dict[str, int],
                runs_dir: Path, target: str) -> Failure:
    """Confirm a failing seed by replay, shrink it, and describe the minimal run."""
    base = runs_dir / scen.name
    kept = base / f"seed-{seed}"
    work = base / f".shrink-{os.getpid()}"
    replays = 0

    def fails(candidate: dict[str, int]) -> bool:
        nonlocal replays
        replays += 1
        return execute(scen, {"explicit": candidate}, work).violated(invariant)

    def failure(entries: list[dict], message: str, small: dict[str, int], problem: str | None):
        path = base / f"seed-{seed}.min.json"
        path.write_text(json.dumps({
            "scenario": scen.name, "file": scen.file, "invariant": invariant, "seed": seed,
            "realized": small, "reproduces": problem is None}, indent=2))
        trace, steps = story(entries)
        shutil.rmtree(work, ignore_errors=True)
        return Failure(scen.name, target, seed, invariant, message, trace, steps, len(small),
                       len(realized), replays, path, problem, (invariant, *kinds(entries)))

    first = execute(scen, {"explicit": realized}, work)
    replays += 1
    original = read_story(kept)
    message = next((v.message for v in first.violations if v.invariant == invariant), "")
    if not first.violated(invariant) or not _consumed(realized, first):
        why = ("replaying its recorded draws " + ("did not fail again"
               if not first.violated(invariant) else "reached only some of the recorded keys")
               + ": the run depends on something shape does not control (time, threads,"
               " an unscripted model), so it cannot be replayed or minimized")
        return failure(original, message or "(did not reproduce)", realized, why)

    small = shrink(realized, fails)
    final_dir = base / f"seed-{seed}.min"
    final = execute(scen, {"explicit": small}, final_dir)
    replays += 1
    message = next((v.message for v in final.violations if v.invariant == invariant), "")
    if not final.violated(invariant) or not _consumed(small, final):
        return failure(read_story(kept), message, realized,
                       "the minimized run diverged on replay, so the minimal trace is not trusted")
    return failure(final.entries, message, small, None)


def investigate_all(scen: api.Scenario, failing: list[tuple[int, str, dict]], runs_dir: Path,
                    target: str, limit: int = MAX_INVESTIGATED) -> Report:
    """Group failing seeds by root cause: the invariant plus the ordered deviations of the
    minimized run. Seeds go fewest deviations first. A seed that still contains every deviation
    of a cause already found (same invariant) joins that cause without being shrunk again; any
    other seed is minimized, which is what finds the next cause."""
    ordered = sorted(failing, key=lambda f: (len(f[2]), f[0]))
    causes: dict[tuple, Cause] = {}
    investigated = skipped = 0
    for seed, invariant, realized in ordered:
        raw = raw_kinds(realized)
        home = next((c for c in causes.values() if c.failure.invariant == invariant
                     and not c.failure.problem and c.deviations <= raw), None)
        if home is not None:
            home.seeds.append(seed)
        elif investigated >= limit:
            skipped += 1
        else:
            found = investigate(scen, seed, invariant, realized, runs_dir, target)
            investigated += 1
            causes.setdefault(found.signature, Cause(found)).seeds.append(seed)
    ranked = sorted(causes.values(), key=lambda c: (-len(c.seeds), c.failure.seed))
    return Report(len(failing), ranked, skipped)


def read_story(workdir: Path) -> list[dict]:
    return read_jsonl(workdir / WORLD) + [d | {"kind": "choice" if "choice" in d else "fault"}
                                          for d in read_jsonl(workdir / REALIZED)]


def replay_file(scen: api.Scenario, path: Path, runs_dir: Path) -> tuple[int, str]:
    """Replay a saved failure. Returns (exit code, text)."""
    data = json.loads(path.read_text())
    out = execute(scen, {"explicit": data["realized"]}, runs_dir / scen.name / "replay")
    invariant = data["invariant"]
    trace, _ = story(out.entries)
    body = "\n".join(f"  {line}" for line in trace)
    if out.violated(invariant):
        message = next(v.message for v in out.violations if v.invariant == invariant)
        diverged = not _consumed(data["realized"], out)
        note = "\n  note: the run diverged from the file" if diverged else ""
        return 1, f"FAIL {scen.name}  invariant={invariant} (replayed)\n  {message}\n{body}{note}"
    if not _consumed(data["realized"], out):
        return 2, f"DIVERGED {scen.name}: the replay did not reach the recorded deviations\n{body}"
    return 0, f"PASS {scen.name}: the recorded run no longer violates {invariant}\n{body}"


def render_flaky(flaky: list[int]) -> str:
    shown = ", ".join(map(str, flaky[:10])) + (", ..." if len(flaky) > 10 else "")
    return (f"FLAKY: {len(flaky)} seeds failed once but did not fail the same way when re-run "
            f"{RERUNS} more times ({shown}). They depend on something shape does not control "
            f"(threads, timers, an unscripted model), so they are not counted as failures and "
            f"were not minimized; their first runs are kept under the runs dir.")


def check(scen: api.Scenario, seeds: int = 100, start: int = 0,
          runs_dir: str | Path = "runs") -> None:
    """Run `seeds` seeds. Raises AssertionError carrying the minimized trace on failure."""
    runs = Path(runs_dir)
    swept = run_sweep(scen, range(start, start + seeds), runs)
    problems = swept.problems(scen)
    if not (swept.failing or swept.flaky or problems):
        return
    parts = [f"{len(swept.failing)} of {seeds} seeds failed"
             + (f", {len(swept.flaky)} flaky" if swept.flaky else ""), swept.cov.render()]
    if swept.failing:
        parts.append(investigate_all(scen, swept.failing, runs, scen.name).render())
    if swept.flaky:
        parts.append(render_flaky(swept.flaky))
    raise AssertionError("\n\n".join(parts + problems))
