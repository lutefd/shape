"""Run the OpenAI Agents SDK example end to end and print its results.

    python matrix.py            # prints the tables, exits 1 if any cell differs from EXPECTED

Two tables, both reproducible from a clean venv:

1. Single-fault cells: one fault injected into the first call of the refund tool
   (`fault:payments.refund#0.0`), everything else nominal. Each cell is the first violated
   invariant, or "ok", and the number of real refunds that landed.
2. A seed sweep per variant (`shape test ... --seeds N`'s own engine), with the failing count.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import checks

from shape.boundary import runner
from shape.boundary.faults import FAULTS
from shape.boundary.invariants import World
from shape.boundary.log import WORLD, read_jsonl

VARIANTS = ["refund_unkeyed", "refund_keyed", "refund_approval_unkeyed", "refund_approval_keyed"]
SEEDS = 300

LANDS_TWICE = "at_most_once"
EXPECTED_CELLS = {
    "refund_unkeyed": {
        "timeout_after_commit": (LANDS_TWICE, 2, 2),
        "connection_reset_after_commit": (LANDS_TWICE, 2, 2),
        "crash_after": (LANDS_TWICE, 2, 2),
    },
    "refund_approval_unkeyed": {
        "timeout_after_commit": (LANDS_TWICE, 2, 2),
        "connection_reset_after_commit": (LANDS_TWICE, 2, 2),
        "crash_after": (LANDS_TWICE, 2, 2),
    },
}
KEYED_AFTER_COMMIT = (None, 1, 2)
EXPECTED_SWEEP = {
    "refund_unkeyed": ("fail", {"at_most_once(payments.refund)"}),
    "refund_approval_unkeyed": ("fail", {"at_most_once(payments.refund)"}),
    "refund_keyed": ("pass", set()),
    "refund_approval_keyed": ("pass", set()),
    "refund_wrong_order_unguarded": ("fail", {"approved_before(payments.refund)"}),
    "refund_wrong_order_guarded": ("pass", set()),
}


def cell(variant: str, fault: str, root: Path) -> tuple[str | None, int, int]:
    """(first violated invariant, real refunds, calls to the refund tool)"""
    scen = getattr(checks, variant)
    key = "fault:payments.refund#0.0"
    out = runner.execute(scen, {"explicit": {key: FAULTS.index(fault) + 1}}, root / variant / fault)
    world = World(out.entries, out.completed, 0, scen.max_restarts)
    calls = sum(1 for e in out.entries if e["kind"] == "effect" and e["op"] == "payments.refund")
    first = out.violations[0].invariant.split("(")[0] if out.violations else None
    return first, len(world.effects("payments.refund")), calls


def evidence(root: Path, bad: list[str]) -> list[str]:
    """Check, from the world log and session DB, the SDK behaviors the table rests on."""

    def effects_of(variant: str, fault: str) -> list[dict]:
        return [
            e
            for e in read_jsonl(root / variant / fault / WORLD)
            if e["kind"] == "effect" and e["op"] == "payments.refund"
        ]

    lines = []
    # 1. a tool that raised: the SDK hands the model a generic error and the model calls it again
    effs = effects_of("refund_unkeyed", "connection_reset_after_commit")
    retried = [e["inc"] for e in effs] == [0, 0]
    lines.append(f"tool raised after the refund landed: model re-issued issue_refund: {retried}")

    # 2. a crash mid-tool: session lost the in-flight refund call, so restart re-issued it
    effs = effects_of("refund_unkeyed", "crash_after")
    lost = [e["inc"] for e in effs] == [0, 1]
    with sqlite3.connect(root / "refund_unkeyed" / "crash_after" / "session.db") as db:
        rows = db.execute("SELECT COUNT(*) FROM agent_messages").fetchone()[0]
    lost = lost and rows >= 4
    lines.append(f"crash mid-tool: restarted run re-issued refund (session items={rows}): {lost}")

    # 3. approval flow crash mid-tool: restarted run resumed the saved RunState and cleaned it up
    effs = effects_of("refund_approval_unkeyed", "crash_after")
    paused = root / "refund_approval_unkeyed" / "crash_after" / "paused-run.json"
    again = [e["inc"] for e in effs] == [0, 1] and not paused.exists()
    lines.append(f"crash mid-tool, approval flow: restarted run resumed saved RunState: {again}")

    for ok, what in [
        (retried, "retry after tool error"),
        (lost, "session lost the refund call"),
        (again, "approved state resumed"),
    ]:
        if not ok:
            bad.append(f"evidence missing: {what}")
    return lines


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="shape-openai-agents-"))
    bad: list[str] = []
    print("single fault on the first refund call -> first violated invariant, real refunds/calls\n")
    width = max(len(f) for f in FAULTS) + 2
    print(f"{'':26}" + "".join(f"{f:<{width}}" for f in FAULTS))
    for variant in VARIANTS:
        cells = []
        for fault in FAULTS:
            got = cell(variant, fault, root)
            default = (
                KEYED_AFTER_COMMIT
                if variant.endswith("_keyed")
                and fault in ("timeout_after_commit", "connection_reset_after_commit", "crash_after")
                else (None, 1, 1)
            )
            want = EXPECTED_CELLS.get(variant, {}).get(fault, default)
            if got != want:
                bad.append(f"{variant} x {fault}: got {got}, expected {want}")
            cells.append(f"{got[0] or 'ok'} {got[1]}/{got[2]}")
        print(f"{variant:26}" + "".join(f"{c:<{width}}" for c in cells))
    print("\nwhat the SDK did, from world.jsonl and session.db\n")
    for line in evidence(root, bad):
        print(line)
    print(f"\nseed sweep, {SEEDS} seeds (random fault kinds + model perturbations)\n")
    for variant, (expect, expected_invs) in EXPECTED_SWEEP.items():
        scen = getattr(checks, variant)
        failing = runner.sweep(scen, range(SEEDS), root / "sweep" / variant, jobs=4)
        names = sorted({f[1] for f in failing})
        print(f"{variant:30} failing {len(failing):>3} of {SEEDS}  {', '.join(names) or '-'}")
        if (expect == "fail") != bool(failing):
            bad.append(f"{variant}: sweep expected {expect}, {len(failing)} failing")
        if failing and set(names) != expected_invs:
            bad.append(f"{variant}: unexpected invariants {names} (expected {expected_invs})")
    shutil.rmtree(root, ignore_errors=True)
    for line in bad:
        print("MISMATCH", line, file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
