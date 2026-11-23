"""Command line: `shape test` (boundary mode) and `shape run | replay | shrink` (simulation)."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

from .choices import Realized
from .invariants import Violation
from .report import render_story, story
from .runtime import SAGAS
from .seal import Escape
from .shrink import shrink
from .simulation import RunResult, replay, run_seed


def _first(result: RunResult) -> Violation:
    return result.violations[0]


def _minimize(saga: str, realized: Realized, invariant: str) -> tuple[Realized, RunResult]:
    def fails(candidate: Realized) -> bool:
        return any(v.invariant == invariant for v in replay(saga, candidate).violations)

    small = shrink(realized, fails)
    final = replay(saga, small)
    return dict(final.realized), final


def _write(path: Path, saga: str, invariant: str, realized: Realized, seed: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"saga": saga, "invariant": invariant, "seed": seed, "realized": realized}, indent=2))


def _report(saga: str, invariant: str, realized: Realized, seed: int | None, label: str,
            out: Path) -> None:
    small, final = _minimize(saga, realized, invariant)
    violation = next(v for v in final.violations if v.invariant == invariant)
    lines = story(final, violation)
    _write(out, saga, invariant, small, seed)
    print(f"FAIL {label}  invariant={invariant}")
    print(f"  minimized to {len(lines)} steps ({len(small)} of {len(realized)} choices):")
    print(render_story(lines))
    print(f"  replay: {out}")


def cmd_run(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    failures: list[tuple[int, RunResult]] = []
    for seed in range(args.start, args.start + args.seeds):
        result = run_seed(args.saga, seed)
        if not result.ok:
            failures.append((seed, result))
    elapsed = time.perf_counter() - started
    print(f"saga={args.saga}  seeds={args.seeds} (from {args.start})  "
          f"failing={len(failures)}  wall={elapsed:.2f}s  {args.seeds / elapsed:.0f} runs/s")
    if not failures:
        print("PASS")
        return 0
    by_invariant = Counter(_first(r).invariant for _, r in failures)
    print("failing seeds by invariant: " + ", ".join(f"{k}={v}" for k, v in by_invariant.items()))
    for invariant in by_invariant:
        seed, result = next((s, r) for s, r in failures if _first(r).invariant == invariant)
        _report(args.saga, invariant, result.realized, seed, f"seed={seed}",
                Path(args.runs_dir) / f"seed-{seed}.{invariant}.min.json")
    return 1


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text())


def cmd_replay(args: argparse.Namespace) -> int:
    data = _load(args.file)
    result = replay(data["saga"], data["realized"])
    print(result.trace.render())
    for v in result.violations:
        print(f"VIOLATION {v.invariant}: {v.message}")
    return 0 if result.ok else 1


def cmd_shrink(args: argparse.Namespace) -> int:
    data = _load(args.file)
    result = replay(data["saga"], data["realized"])
    if result.ok:
        print("run does not fail; nothing to shrink")
        return 0
    invariant = data.get("invariant") or _first(result).invariant
    out = Path(args.file).with_suffix(".min.json")
    _report(data["saga"], invariant, data["realized"], data.get("seed"), Path(args.file).name, out)
    return 0


def _import_failure(path: str, exc: BaseException) -> str:
    """One clean line for a scenario file that does not import, with where in the file it broke."""
    from .boundary.rootcause import describe

    here = Path(path).resolve().parent
    if isinstance(exc, SyntaxError) and exc.lineno:
        where = f" ({Path(exc.filename or path).name}:{exc.lineno})"
    else:
        mine = [f for f in traceback.extract_tb(exc.__traceback__)
                if Path(f.filename).resolve().is_relative_to(here)]
        where = f" ({Path(mine[-1].filename).name}:{mine[-1].lineno})" if mine else ""
    return (f"shape: cannot import {path}: {describe(exc)}{where}\n"
            f"(SHAPE_TRACEBACK=1 shows the full traceback)")


def _load_scenarios(path: str) -> dict | None:
    from .boundary.child import load_module, scenarios_in

    if not Path(path).is_file():
        print(f"shape: no such file: {path}", file=sys.stderr)
        return None
    try:
        return scenarios_in(load_module(path))
    except (Exception, SystemExit) as exc:  # the user's module can fail any way
        if os.environ.get("SHAPE_TRACEBACK"):
            raise
        print(_import_failure(path, exc), file=sys.stderr)
        return None


def cmd_test(args: argparse.Namespace) -> int:
    from .boundary import runner

    try:
        deadline = runner.deadline_for(args.budget)
        if args.max_shrinks is not None and args.max_shrinks < 0:
            raise ValueError("max-shrinks must be non-negative")
    except ValueError as exc:
        print(f"shape: {exc}", file=sys.stderr)
        return 2
    path, _, name = args.target.partition("::")
    with contextlib.redirect_stdout(sys.stderr) if args.json else contextlib.nullcontext():
        found = _load_scenarios(path)
    if found is None:
        return 2
    if name and name not in found:
        known = ", ".join(found) or "none"
        print(f"no scenario {name!r} in {path}; found: {known}", file=sys.stderr)
        return 2
    chosen = {name: found[name]} if name else found
    if not chosen:
        print(f"no @shape.scenario in {path}", file=sys.stderr)
        return 2
    runs = Path(args.runs_dir)
    if args.replay:
        replayed = Path(args.replay)
        scen = chosen[name] if name else found[json.loads(replayed.read_text())["scenario"]]
        if args.strict_scopes:
            scen = dataclasses.replace(scen, strict_scopes=True)
        code, text = runner.replay_file(scen, replayed, runs)
        print(text)
        return code
    failed = False
    reports = []
    for scen in chosen.values():
        if args.strict_scopes:
            scen = dataclasses.replace(scen, strict_scopes=True)
        with contextlib.redirect_stdout(sys.stderr) if args.json else contextlib.nullcontext():
            swept = runner.run_sweep(
                scen, range(args.start, args.start + args.seeds), runs, args.jobs, deadline=deadline,
            )
            report = runner.Report(0, [])
            if swept.failing:
                limit = runner.MAX_INVESTIGATED if args.max_shrinks is None else args.max_shrinks
                report = runner.investigate_all(
                    scen, swept.failing, runs, f"{path}::{scen.name}", limit=limit,
                    defect_keys=swept.defect_keys, deadline=deadline,
                )
        problems = swept.problems(scen)
        notes = swept.notes(scen)
        coverage = swept.cov.render()
        scopes = swept.cov.render_scopes(scen.invariants)
        scenario_failed = bool(swept.failing or swept.flaky or problems)
        failed |= scenario_failed
        if args.json:
            reports.append({
                "scenario": scen.name,
                "target": f"{path}::{scen.name}",
                "status": "fail" if scenario_failed else "pass",
                "start": args.start,
                "seeds": swept.seeds,
                "failing_seeds": sorted(seed for seed, _, _ in swept.failing),
                "flaky_seeds": sorted(swept.flaky),
                "flaky_details": [
                    swept.flaky_details[s].as_dict() for s in sorted(swept.flaky_details)
                ],
                "skipped_seeds": swept.skipped_seeds,
                "unconfirmed_seeds": sorted(swept.unconfirmed),
                "problems": problems,
                "notes": notes,
                "coverage": coverage,
                "scope_assignment": scopes,
                **report.as_dict(),
            })
            continue
        flaky = f"  flaky={len(swept.flaky)}" if swept.flaky else ""
        print(f"scenario={scen.name}  seeds={swept.seeds} (from {args.start})  "
              f"failing={len(swept.failing)}{flaky}  wall={swept.wall:.2f}s  "
              f"{swept.seeds / swept.wall:.0f} seeds/s")
        print(coverage)
        if scopes:
            print(scopes)
        if args.verbose:
            for note in notes:
                print(note)
        if swept.failing:
            print(report.render(all_triggers=args.all_triggers))
        if swept.flaky:
            print(runner.render_flaky(swept.flaky, swept.flaky_details))
        for problem in problems:
            print(problem)
        if not scenario_failed:
            print("PASS")
    if args.json:
        print(json.dumps({"scenarios": reports}, indent=2))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    from .fakes.contracts import command as contract_command
    from .fakes.contracts._suites import CONTRACTS

    parser = argparse.ArgumentParser(prog="shape")
    sub = parser.add_subparsers(dest="command", required=True)
    test = sub.add_parser("test", help="boundary mode: test your own agent code")
    test.add_argument("target", help="file.py or file.py::scenario")
    test.add_argument("--seeds", type=int, default=100)
    test.add_argument("--start", type=int, default=0)
    test.add_argument("--replay", help="replay a saved .min.json instead of running seeds")
    test.add_argument("--all-triggers", action="store_true",
                      help="show a minimized trace for every trigger class")
    test.add_argument("--json", action="store_true",
                      help="emit a JSON sweep report including replay paths")
    test.add_argument("--jobs", type=int, default=1, help="seeds to run in parallel")
    test.add_argument("--budget", type=float, metavar="SECONDS",
                      help="wall time for seeds, confirmations and shrinking")
    test.add_argument("--max-shrinks", type=int, metavar="N",
                      help="maximum failing seeds to minimize (default: 200)")
    test.add_argument("--strict-scopes", action="store_true",
                      help="refuse default/derived scopes in duplicate-sensitive invariants")
    test.add_argument("-v", "--verbose", action="store_true",
                      help="show diagnostic notes such as watched ops that no seed called")
    test.add_argument("--runs-dir", default="runs")
    test.set_defaults(fn=cmd_test)
    run = sub.add_parser("run", help="run many seeds, shrink the first failure of each kind")
    run.add_argument("--seeds", type=int, default=1000)
    run.add_argument("--start", type=int, default=0)
    run.add_argument("--saga", choices=sorted(SAGAS), required=True)
    run.add_argument("--runs-dir", default="runs")
    run.set_defaults(fn=cmd_run)
    rep = sub.add_parser("replay", help="replay a saved run and print its trace")
    rep.add_argument("file")
    rep.set_defaults(fn=cmd_replay)
    shr = sub.add_parser("shrink", help="minimize a saved failing run")
    shr.add_argument("file")
    shr.set_defaults(fn=cmd_shrink)
    fakes = sub.add_parser("fakes", help="provider fake utilities")
    fakes_sub = fakes.add_subparsers(dest="fakes_command", required=True)
    contract = fakes_sub.add_parser("contract", help="check a fake against its provider contract")
    contract.add_argument("name", choices=sorted(CONTRACTS))
    contract.add_argument("--live", action="store_true", help="compare with a configured test service")
    contract.add_argument("--report", metavar="FILE", help="write a JSON report with contract evidence")
    contract.set_defaults(fn=contract_command)
    args = parser.parse_args(argv)
    if args.command == "test" and args.replay and (args.json or args.all_triggers):
        test.error("--json and --all-triggers are only available for sweeps")
    try:
        code = args.fn(args)
        sys.stdout.flush()
        return code
    except Escape as escape:
        print(f"ESCAPE: {escape}", file=sys.stderr)
        return 2
    except BrokenPipeError:  # `shape test ... | head`: the reader left, which is not an error
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())  # no flush error at exit
        return 141


if __name__ == "__main__":
    sys.exit(main())
