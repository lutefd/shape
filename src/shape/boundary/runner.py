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
from . import api
from .faults import CRASH_EXIT, FAULT_TEXT
from .invariants import Violation, World, scenario_terminates
from .log import PARENT_SEQ, REALIZED, WORLD, JsonlWriter, read_jsonl

INCARNATION_TIMEOUT = 30.0  # seconds; a hung incarnation counts as one that never finished
TIMEOUT_EXIT = 124


@dataclass
class Outcome:
    dir: Path
    completed: bool
    entries: list[dict]
    realized: dict[str, int]
    violations: list[Violation]

    @property
    def ok(self) -> bool:
        return not self.violations

    def violated(self, name: str) -> bool:
        return any(v.invariant == name for v in self.violations)


def make_choices(spec: dict) -> ChoiceSource:
    return SeededChoices(spec["seed"]) if "seed" in spec else ExplicitChoices(spec["explicit"])


# ---------------------------------------------------------------------------------- incarnations


def child_main(scen: api.Scenario, spec: dict, workdir: Path, inc: int) -> int:
    """Run one incarnation inside the current (already isolated) process. Returns its exit code."""
    out = os.open(workdir / f"inc-{inc}.out", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.dup2(out, 1)
    os.dup2(out, 2)
    sys.stdout = sys.stderr = os.fdopen(1, "w", buffering=1, closefd=False)
    ctx = api._Context(workdir, inc, make_choices(spec))
    api.activate(ctx)
    watchdog = threading.Timer(INCARNATION_TIMEOUT, lambda: os._exit(TIMEOUT_EXIT))
    watchdog.daemon = True
    watchdog.start()
    try:
        scen.fn(api.Run(workdir, inc, spec.get("seed")))
        return 0
    except BaseException:  # noqa: BLE001 - anything that kills the agent is a death to log
        text = traceback.format_exc()
        ctx.append(ctx.world, {"kind": "error", "text": text.strip().splitlines()[-1]})
        sys.stderr.write(text)
        return 1


def _classify(code: int) -> str:
    return {0: "done", CRASH_EXIT: "crash", TIMEOUT_EXIT: "timeout"}.get(code, "error")


def _fork_incarnation(scen: api.Scenario, spec: dict, workdir: Path, inc: int) -> int:
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        code = 70
        try:
            code = child_main(scen, spec, workdir, inc)
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status) if not os.WIFSIGNALED(status) else 1


def _spawn_incarnation(scen: api.Scenario, spec: dict, workdir: Path, inc: int) -> int:
    payload = json.dumps({"file": scen.file, "name": scen.name, "spec": spec,
                          "workdir": str(workdir), "inc": inc})
    root = str(Path(__file__).resolve().parents[2])
    paths = [root, *filter(None, [os.environ.get("PYTHONPATH")])]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(paths)}
    proc = subprocess.run([sys.executable, "-m", "shape.boundary.child", payload],
                          env=env, check=False)
    return proc.returncode if proc.returncode >= 0 else 1


def _incarnate(scen: api.Scenario, spec: dict, workdir: Path, inc: int) -> int:
    use_fork = hasattr(os, "fork") and os.environ.get("SHAPE_START_METHOD") != "spawn"
    return (_fork_incarnation if use_fork else _spawn_incarnation)(scen, spec, workdir, inc)


# ------------------------------------------------------------------------------------------ a seed


def execute(scen: api.Scenario, spec: dict, workdir: Path) -> Outcome:
    """Run one seed (or one explicit replay) in `workdir`, restarting after every death."""
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    parent = JsonlWriter(workdir / WORLD, fsync=False)
    completed, incarnations = False, 0
    for inc in range(scen.max_restarts + 1):
        incarnations += 1
        how = _classify(_incarnate(scen, spec, workdir, inc))
        parent.write({"kind": "incarnation", "inc": inc, "seq": PARENT_SEQ, "how": how})
        if how == "done":
            completed = True
            break
    parent.close()
    entries = read_jsonl(workdir / WORLD)
    draws = read_jsonl(workdir / REALIZED)
    world = World(entries, completed, incarnations, scen.max_restarts)
    violations = []
    for inv in [scenario_terminates(), *scen.invariants]:
        message = inv.check(world)
        if message:
            violations.append(Violation(inv.name, message))
    return Outcome(workdir, completed, entries + [d | {"kind": "fault"} for d in draws],
                   {d["key"]: d["value"] for d in draws}, violations)


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


def sweep(scen: api.Scenario, seeds: range, runs_dir: Path,
          jobs: int = 1) -> list[tuple[int, str, dict]]:
    """Run every seed. Returns (seed, first violated invariant, realized) for each failure."""
    def one(seed: int) -> list:
        out = run_seed(scen, seed, runs_dir)
        return [] if out.ok else [seed, out.violations[0].invariant, out.realized]

    if jobs <= 1 or not hasattr(os, "fork"):
        results = [one(s) for s in seeds]
    else:
        results = _parallel(list(seeds), jobs, one)
    return [tuple(r) for r in results if r]


def _parallel(seeds: list[int], jobs: int, one) -> list:
    workers = []
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
              if e["kind"] in ("effect", "approval", "error", "incarnation")
              or (e["kind"] == "fault" and e["fault"] not in api.AFTER_COMMIT)]
    events.sort(key=lambda e: (e["inc"], e["seq"]))
    lines: list[str] = []
    steps, current = 0, -1
    for e in events:
        if e["inc"] != current:
            current = e["inc"]
            lines.append(f"incarnation {current}" + (" (restarted)" if current else ""))
        kind = e["kind"]
        if kind in ("effect", "fault"):
            steps += 1
            fault = e["fault"]
            note = FAULT_TEXT[fault].format(exc=e["raised"]) if fault else ""
            if kind == "effect":
                landed = "effect landed" + (f" (idempotency key {e['key']!r})" if e["key"] else "")
                note = f"{landed}, then {note}" if fault else landed
            lines += [f"  {steps}. {e['call']}", f"       {note}"]
        elif kind == "approval":
            steps += 1
            lines.append(f"  {steps}. approval recorded for {e['scope']!r}")
        elif kind == "error":
            lines.append(f"  process raised: {e['text']}")
        elif e["how"] == "done":
            lines.append("  finished")
        elif e["how"] == "timeout":
            lines.append(f"  killed after {INCARNATION_TIMEOUT:.0f}s without finishing")
        elif e["how"] == "crash":
            lines.append("  -> process died")
    return lines, steps


@dataclass
class Failure:
    scenario: str
    target: str
    seed: int
    invariant: str
    message: str
    trace: list[str]
    steps: int
    faults: int
    original_faults: int
    replays: int
    replay_file: Path | None
    problem: str | None = None  # set when the run does not reproduce: no minimality claim then
    extra: list[str] = field(default_factory=list)

    def render(self) -> str:
        head = f"FAIL {self.target}  seed={self.seed}  invariant={self.invariant}"
        out = [head, f"  {self.message}"]
        if self.problem:
            out += [f"  NOT MINIMIZED: {self.problem}",
                    f"  original run ({self.original_faults} faults) as it happened:"]
        else:
            out.append(f"  minimized to {self.steps} steps ({self.faults} of {self.original_faults}"
                       f" faults kept, {self.replays} replays):")
        out += [f"    {line}" for line in self.trace]
        if self.replay_file:
            out.append(f"  replay: {self.replay_file}")
        return "\n".join(out)


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
                       len(realized), replays, path, problem)

    first = execute(scen, {"explicit": realized}, work)
    replays += 1
    original = read_story(kept)
    message = next((v.message for v in first.violations if v.invariant == invariant), "")
    if not first.violated(invariant) or not _consumed(realized, first):
        why = ("replaying its recorded fault draws " + ("did not fail again"
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


def read_story(workdir: Path) -> list[dict]:
    return read_jsonl(workdir / WORLD) + [d | {"kind": "fault"}
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
        return 2, f"DIVERGED {scen.name}: the replay did not reach the recorded faults\n{body}"
    return 0, f"PASS {scen.name}: the recorded run no longer violates {invariant}\n{body}"


def check(scen: api.Scenario, seeds: int = 100, start: int = 0,
          runs_dir: str | Path = "runs") -> None:
    """Run `seeds` seeds. Raises AssertionError carrying the minimized trace on failure."""
    runs = Path(runs_dir)
    failing = sweep(scen, range(start, start + seeds), runs)
    if not failing:
        return
    reports = []
    for invariant in dict.fromkeys(f[1] for f in failing):
        seed, _, realized = pick(failing, invariant)
        reports.append(investigate(scen, seed, invariant, realized, runs, scen.name).render())
    raise AssertionError(f"{len(failing)} of {seeds} seeds failed\n\n" + "\n\n".join(reports))


def pick(failing: list[tuple[int, str, dict]], invariant: str) -> tuple[int, str, dict]:
    """The failing seed worth shrinking: fewest faults, then lowest seed."""
    return min((f for f in failing if f[1] == invariant), key=lambda f: (len(f[2]), f[0]))


def timed_sweep(scen: api.Scenario, seeds: range, runs_dir: Path, jobs: int):
    started = time.perf_counter()
    failing = sweep(scen, seeds, runs_dir, jobs)
    return failing, time.perf_counter() - started
