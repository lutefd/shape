"""Run the OpenAI Agents SDK example end to end and print its results.

    python matrix.py            # prints the tables, exits 1 if any cell differs from EXPECTED

Two tables, both reproducible from a clean venv:

1. Single-fault cells: one fault injected into the first call of the refund tool's payments call
   (`fault:payments.refund#0.0`), everything else nominal. Each cell is the first violated
   invariant, or "ok", and the number of real refunds that landed.
2. A seed sweep per variant (`shape test ... --seeds N`'s own engine), with the failing count.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import checks

from shape.boundary import runner
from shape.boundary.faults import FAULTS
from shape.boundary.invariants import World

VARIANTS = ["refund_unkeyed", "refund_keyed", "refund_approval_unkeyed", "refund_approval_keyed"]
SEEDS = 300

# What the SDK's documented behaviour, plus the code in agent.py, should produce.
LANDS_TWICE = "at_most_once"
EXPECTED_CELLS = {
    # variant: {fault: (first violated invariant or None, real refunds, calls that landed)}
    "refund_unkeyed": {"timeout_after_commit": (LANDS_TWICE, 2, 2),
                       "connection_reset_after_commit": (LANDS_TWICE, 2, 2),
                       "crash_after": (LANDS_TWICE, 2, 2)},
    "refund_approval_unkeyed": {"timeout_after_commit": (LANDS_TWICE, 2, 2),
                                "connection_reset_after_commit": (LANDS_TWICE, 2, 2),
                                "crash_after": (LANDS_TWICE, 2, 2)},
}
# Keyed variants with a fault after the commit land two calls but one real refund (same key).
KEYED_AFTER_COMMIT = (None, 1, 2)
# Every other cell must be (None, 1, 1): no violation, one refund, one call.
EXPECTED_SWEEP = {"refund_unkeyed": "fail", "refund_approval_unkeyed": "fail",
                  "refund_keyed": "pass", "refund_approval_keyed": "pass"}


def cell(variant: str, fault: str, root: Path) -> tuple[str | None, int, int]:
    """(first violated invariant, real refunds, calls to the payments client)"""
    scen = getattr(checks, variant)
    key = "fault:payments.refund#0.0"
    out = runner.execute(scen, {"explicit": {key: FAULTS.index(fault) + 1}}, root / variant / fault)
    world = World(out.entries, out.completed, 0, scen.max_restarts)
    calls = sum(1 for e in out.entries if e["kind"] == "effect" and e["op"] == "payments.refund")
    first = out.violations[0].invariant.split("(")[0] if out.violations else None
    return first, len(world.effects("payments.refund")), calls


def evidence(root: Path, bad: list[str]) -> list[str]:
    """Check, from each incarnation's output, the two SDK behaviours the table rests on."""
    def read(variant: str, fault: str, inc: int) -> str:
        return (root / variant / fault / f"inc-{inc}.out").read_text()

    lines = []
    # 1. a tool that raised: the SDK hands the model a generic error and the model calls it again
    text = read("refund_unkeyed", "connection_reset_after_commit", 0)
    retried = text.count("-> issue_refund(") == 2 and "final message" in text
    lines.append(f"tool raised after the refund landed: model re-issued issue_refund: {retried}")
    # 2. a crash while the tool runs: the session never saw the refund call, so the resumed run's
    #    model sees only the lookup and the redelivered message (4 items) and refunds again
    text = read("refund_unkeyed", "crash_after", 1)
    lost = "sees 4 items -> issue_refund(" in text
    lines.append(f"crash mid-tool: resumed model saw 4 session items and re-issued the refund: {lost}")
    # 3. the same crash with the paused RunState on disk: the approved call is executed again
    text = read("refund_approval_unkeyed", "crash_after", 1)
    again = "final message" in text and "issue_refund" not in text
    lines.append(f"crash mid-tool, approval flow: restarted run resumed the saved RunState: {again}")
    for ok, what in [(retried, "retry after tool error"), (lost, "session lost the refund call"),
                     (again, "approved state resumed")]:
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
            default = KEYED_AFTER_COMMIT if variant.endswith("_keyed") and fault in (
                "timeout_after_commit", "connection_reset_after_commit", "crash_after") else (
                None, 1, 1)
            want = EXPECTED_CELLS.get(variant, {}).get(fault, default)
            if got != want:
                bad.append(f"{variant} x {fault}: got {got}, expected {want}")
            cells.append(f"{got[0] or 'ok'} {got[1]}/{got[2]}")
        print(f"{variant:26}" + "".join(f"{c:<{width}}" for c in cells))
    print("\nwhat the SDK did, from the agent's own output (inc-N.out)\n")
    for line in evidence(root, bad):
        print(line)
    print(f"\nseed sweep, {SEEDS} seeds (random fault kinds, up to 3 restarts)\n")
    for variant in VARIANTS:
        scen = getattr(checks, variant)
        failing = runner.sweep(scen, range(SEEDS), root / "sweep" / variant)
        names = sorted({f[1] for f in failing})
        print(f"{variant:26} failing {len(failing):>3} of {SEEDS}  {', '.join(names) or '-'}")
        if (EXPECTED_SWEEP[variant] == "fail") != bool(failing):
            bad.append(f"{variant}: sweep expected {EXPECTED_SWEEP[variant]}, {len(failing)} failing")
        if failing and set(names) != {"at_most_once(payments.refund)"}:
            bad.append(f"{variant}: unexpected invariants {names}")
    shutil.rmtree(root, ignore_errors=True)
    for line in bad:
        print("MISMATCH", line, file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
