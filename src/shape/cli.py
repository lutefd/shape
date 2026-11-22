"""Command line: `shape run | replay | shrink`."""

from __future__ import annotations

import argparse
import json
import sys
import time
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shape")
    sub = parser.add_subparsers(dest="command", required=True)
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
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except Escape as escape:
        print(f"ESCAPE: {escape}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
