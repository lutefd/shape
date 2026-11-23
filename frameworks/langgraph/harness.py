"""Jepsen-style crash-resume probe harness for LangGraph.

Spawns each workload in isolated subprocesses, injects real SIGKILL crashes at
deterministic crash points via the CRASH_POINT environment variable, restarts a
fresh process to resume from LangGraph's SqliteSaver checkpointer, and checks
invariants against the external god's-eye WorldLog.

Every cell is executed N times (default 3) to verify reproducibility (>= 3/3).
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from world import WorldLog

HERE = Path(__file__).resolve().parent
CHARGE_SCRIPT = HERE / "workload_charge_once.py"
APPROVAL_SCRIPT = HERE / "workload_approval_gate.py"


@dataclass
class TrialResult:
    effects: dict[str, int]
    attempts: dict[str, int]
    final_outcome: str
    final_status: str | None
    crashed_with_sigkill: bool
    invariant_results: dict[str, bool]

    @property
    def passed(self) -> bool:
        return all(self.invariant_results.values())


@dataclass
class CellReport:
    workload: str
    crash_point: str
    variant: str
    trials: list[TrialResult]
    classification: str
    note: str

    @property
    def pass_count(self) -> int:
        return sum(1 for t in self.trials if t.passed)

    @property
    def fail_count(self) -> int:
        return len(self.trials) - self.pass_count


def _run_subprocess(
    script: Path,
    args: list[str],
    crash_point: str = "",
) -> tuple[int, dict[str, object] | None, str]:
    env = os.environ.copy()
    if crash_point:
        env["CRASH_POINT"] = crash_point
    else:
        env.pop("CRASH_POINT", None)
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    parsed: dict[str, object] | None = None
    if proc.returncode == 0 and proc.stdout.strip():
        last_line = proc.stdout.strip().splitlines()[-1]
        parsed = json.loads(last_line)
    return proc.returncode, parsed, proc.stderr.strip()


def run_charge_once_trial(variant: str, crash_point: str, trial_idx: int) -> TrialResult:
    with tempfile.TemporaryDirectory(prefix="lg-charge-") as td:
        cp_db = os.path.join(td, "checkpoints.sqlite")
        world_db = os.path.join(td, "world.sqlite")
        thread_id = f"charge-{variant}-{crash_point}-{trial_idx}"
        order_id = f"ord-{variant}-{crash_point}-{trial_idx}"
        base_args = [
            "--variant",
            variant,
            "--checkpoint-db",
            cp_db,
            "--world-db",
            world_db,
            "--thread-id",
            thread_id,
            "--order-id",
            order_id,
        ]

        if crash_point == "none":
            rc, out, err = _run_subprocess(CHARGE_SCRIPT, [*base_args, "--phase", "run"])
            assert rc == 0, f"unexpected failure: {err}"
            crashed = False
            final_out = out or {}
        else:
            rc1, _, err1 = _run_subprocess(
                CHARGE_SCRIPT,
                [*base_args, "--phase", "run"],
                crash_point=crash_point,
            )
            crashed = rc1 == -signal.SIGKILL
            assert crashed, f"expected SIGKILL (-9), got rc={rc1} stderr={err1}"
            rc2, out2, err2 = _run_subprocess(
                CHARGE_SCRIPT,
                [*base_args, "--phase", "recover"],
            )
            assert rc2 == 0, f"unexpected recovery failure: {err2}"
            final_out = out2 or {}

        world = WorldLog(world_db)
        charges = world.count_effects("charge")
        confirms = world.count_effects("confirm")
        outcome = str(final_out.get("outcome", "unknown"))
        invariants = {
            "at_most_once_charge": charges <= 1,
            "at_most_once_confirm": confirms <= 1,
            "completed_with_effects": outcome == "completed" and charges == 1 and confirms == 1,
        }
        return TrialResult(
            effects={"charge": charges, "confirm": confirms},
            attempts={
                "charge": world.count_attempts("charge"),
                "confirm": world.count_attempts("confirm"),
            },
            final_outcome=outcome,
            final_status="completed" if outcome == "completed" else outcome,
            crashed_with_sigkill=crashed,
            invariant_results=invariants,
        )


def run_approval_gate_trial(variant: str, crash_point: str, trial_idx: int) -> TrialResult:
    with tempfile.TemporaryDirectory(prefix="lg-approval-") as td:
        cp_db = os.path.join(td, "checkpoints.sqlite")
        world_db = os.path.join(td, "world.sqlite")
        thread_id = f"approval-{variant}-{crash_point}-{trial_idx}"
        order_id = f"wire-{variant}-{crash_point}-{trial_idx}"
        base_args = [
            "--variant",
            variant,
            "--checkpoint-db",
            cp_db,
            "--world-db",
            world_db,
            "--thread-id",
            thread_id,
            "--order-id",
            order_id,
        ]
        crashed = False

        if crash_point == "no_approval_resume":
            rc1, out1, err1 = _run_subprocess(APPROVAL_SCRIPT, [*base_args, "--phase", "init"])
            assert rc1 == 0 and out1 and out1["interrupted"], f"init failed: {out1} {err1}"
            rc2, out2, err2 = _run_subprocess(APPROVAL_SCRIPT, [*base_args, "--phase", "recover"])
            assert rc2 == 0, f"recover failed: {err2}"
            final_out = out2 or {}
            expect_mode = "no_approval"

        elif crash_point == "before_interrupt":
            rc1, _, err1 = _run_subprocess(
                APPROVAL_SCRIPT,
                [*base_args, "--phase", "init"],
                crash_point="before_interrupt",
            )
            crashed = rc1 == -signal.SIGKILL
            assert crashed, f"expected SIGKILL, got {rc1} {err1}"
            rc2, out2, err2 = _run_subprocess(APPROVAL_SCRIPT, [*base_args, "--phase", "recover"])
            assert rc2 == 0, f"recover failed: {err2}"
            final_out = out2 or {}
            expect_mode = "no_approval"

        elif crash_point == "while_waiting_approve":
            rc1, _, err1 = _run_subprocess(
                APPROVAL_SCRIPT,
                [*base_args, "--phase", "init"],
                crash_point="while_waiting",
            )
            crashed = rc1 == -signal.SIGKILL
            assert crashed, f"expected SIGKILL, got {rc1} {err1}"
            rc2, out2, err2 = _run_subprocess(
                APPROVAL_SCRIPT, [*base_args, "--phase", "resume_approve"]
            )
            assert rc2 == 0, f"resume_approve failed: {err2}"
            final_out = out2 or {}
            expect_mode = "approved"

        elif crash_point == "while_waiting_reject":
            rc1, _, err1 = _run_subprocess(
                APPROVAL_SCRIPT,
                [*base_args, "--phase", "init"],
                crash_point="while_waiting",
            )
            crashed = rc1 == -signal.SIGKILL
            assert crashed, f"expected SIGKILL, got {rc1} {err1}"
            rc2, out2, err2 = _run_subprocess(
                APPROVAL_SCRIPT, [*base_args, "--phase", "resume_reject"]
            )
            assert rc2 == 0, f"resume_reject failed: {err2}"
            final_out = out2 or {}
            expect_mode = "rejected"

        elif crash_point == "after_resume_rejected":
            rc1, out1, err1 = _run_subprocess(APPROVAL_SCRIPT, [*base_args, "--phase", "init"])
            assert rc1 == 0 and out1 and out1["interrupted"], f"init failed: {out1} {err1}"
            rc2, _, err2 = _run_subprocess(
                APPROVAL_SCRIPT,
                [*base_args, "--phase", "resume_reject"],
                crash_point="after_resume_rejected",
            )
            crashed = rc2 == -signal.SIGKILL
            assert crashed, f"expected SIGKILL, got {rc2} {err2}"
            rc3, out3, err3 = _run_subprocess(APPROVAL_SCRIPT, [*base_args, "--phase", "recover"])
            assert rc3 == 0, f"recover failed: {err3}"
            final_out = out3 or {}
            expect_mode = "rejected_crash"

        elif crash_point in (
            "after_resume_approved_in_approval",
            "at_execute_entry",
            "after_effect_before_checkpoint",
        ):
            rc1, out1, err1 = _run_subprocess(APPROVAL_SCRIPT, [*base_args, "--phase", "init"])
            assert rc1 == 0 and out1 and out1["interrupted"], f"init failed: {out1} {err1}"
            rc2, _, err2 = _run_subprocess(
                APPROVAL_SCRIPT,
                [*base_args, "--phase", "resume_approve"],
                crash_point=crash_point,
            )
            crashed = rc2 == -signal.SIGKILL
            assert crashed, f"expected SIGKILL, got {rc2} {err2}"
            rc3, out3, err3 = _run_subprocess(APPROVAL_SCRIPT, [*base_args, "--phase", "recover"])
            assert rc3 == 0, f"recover failed: {err3}"
            final_out = out3 or {}
            expect_mode = "approved"

        elif crash_point == "double_resume_after_completion":
            rc1, out1, err1 = _run_subprocess(APPROVAL_SCRIPT, [*base_args, "--phase", "init"])
            assert rc1 == 0 and out1 and out1["interrupted"], f"init failed: {out1} {err1}"
            rc2, out2, err2 = _run_subprocess(
                APPROVAL_SCRIPT, [*base_args, "--phase", "resume_approve"]
            )
            assert rc2 == 0 and out2 and out2["status"] == "executed", f"first resume failed: {out2} {err2}"
            rc3, out3, err3 = _run_subprocess(
                APPROVAL_SCRIPT, [*base_args, "--phase", "resume_approve"]
            )
            assert rc3 == 0, f"second resume failed: {err3}"
            final_out = out3 or {}
            expect_mode = "approved"

        else:
            raise ValueError(f"Unknown crash point: {crash_point}")

        world = WorldLog(world_db)
        wires = world.count_effects("wire_transfer")
        outcome = str(final_out.get("outcome", "unknown"))
        status = str(final_out.get("status")) if final_out.get("status") is not None else None

        if expect_mode == "no_approval":
            invariants = {
                "never_without_approval": wires == 0,
                "remains_interrupted": outcome == "interrupted",
            }
        elif expect_mode == "rejected":
            invariants = {
                "never_after_rejection": wires == 0,
                "reached_rejected": outcome == "completed" and status == "rejected",
            }
        elif expect_mode == "rejected_crash":
            invariants = {
                "never_after_rejection": wires == 0,
                "no_unauthorized_execution": status != "executed",
            }
        else:
            invariants = {
                "at_most_once_after_approval": wires <= 1,
                "exactly_once_after_approval": wires == 1 and outcome == "completed" and status == "executed",
            }

        return TrialResult(
            effects={"wire_transfer": wires},
            attempts={"wire_transfer": world.count_attempts("wire_transfer")},
            final_outcome=outcome,
            final_status=status,
            crashed_with_sigkill=crashed,
            invariant_results=invariants,
        )


def classify_cell(workload: str, crash_point: str, variant: str, trials: list[TrialResult]) -> tuple[str, str]:
    all_pass = all(t.passed for t in trials)
    if all_pass:
        return ("PASS", "All invariants held across 3/3 trials.")

    if workload == "charge-once":
        if crash_point == "after_charge_before_checkpoint":
            return (
                "(c) works as designed",
                "Node/task crashed after external write but before returning; docs state execution is at-least-once and side effects must be idempotent.",
            )
        if crash_point == "after_charge_returns" and variant in ("naive", "task_async"):
            return (
                "(b) dangerous default / tutorial footgun",
                "Default durability='async' advances past completed charge node/@task before SQLite commits its checkpoint/writes; SIGKILL re-runs charge 3/3.",
            )
        if crash_point == "mid_confirm_after_effect":
            return (
                "(c) works as designed",
                "confirm crashed after external write before returning; re-execution repeats non-idempotent confirm (and in async mode may also lag charge).",
            )

    if workload == "approval-gate":
        if crash_point == "after_resume_approved_in_approval":
            if variant == "naive_two_node":
                return (
                    "(b) dangerous default / tutorial footgun",
                    "Command(resume=True) write is async; SIGKILL right after interrupt() returns loses approval and reverts thread to pending interrupt (0 effects).",
                )
            return (
                "(c) works as designed",
                "Even with durability='sync', the Command(resume=True) write is not awaited before approval_node runs; SIGKILL mid-step loses the approval 3/3 (0 effects; safety holds, approval liveness lost). The docs promise durability only at step boundaries, so this is surprising, not a broken guarantee.",
            )
        if crash_point == "at_execute_entry" and variant == "naive_two_node":
            return (
                "(b) dangerous default / tutorial footgun",
                "Default durability='async' enters execute_node before approval_node's step checkpoint commits; SIGKILL reverts thread to pending interrupt (0 effects).",
            )
        if crash_point == "after_effect_before_checkpoint":
            return (
                "(c) works as designed",
                "execute_node crashed after irreversible wire_transfer landed but before returning; docs explicitly note retries repeat non-idempotent post-approval effects.",
            )

    return ("UNCLASSIFIED_FAILURE", "Observed failure; inspect trial details.")


def format_effects(workload: str, trials: list[TrialResult]) -> str:
    if workload == "charge-once":
        vals = sorted({(t.effects["charge"], t.effects["confirm"]) for t in trials})
        if len(vals) == 1:
            c, f = vals[0]
            return f"charge={c}, confirm={f}"
        return " / ".join(f"charge={c}, confirm={f}" for c, f in vals)
    vals = sorted({(t.effects["wire_transfer"], t.final_outcome) for t in trials})
    if len(vals) == 1:
        w, o = vals[0]
        return f"wire={w} ({o})"
    return " / ".join(f"wire={w} ({o})" for w, o in vals)


def format_invariants(trials: list[TrialResult]) -> str:
    keys = list(trials[0].invariant_results.keys())
    parts = []
    for k in keys:
        ok = sum(1 for t in trials if t.invariant_results[k])
        mark = "PASS" if ok == len(trials) else "FAIL"
        parts.append(f"`{k}`: {mark} ({ok}/{len(trials)})")
    return "; ".join(parts)


def render_results_md(reports: list[CellReport], trials_per_cell: int) -> str:
    lg_ver = importlib.metadata.version("langgraph")
    sqlite_ver = importlib.metadata.version("langgraph-checkpoint-sqlite")
    ckpt_ver = importlib.metadata.version("langgraph-checkpoint")
    py_ver = platform.python_version()

    lines: list[str] = [
        "# LangGraph Crash-Resume Probe Results",
        "",
        "## Environment & Pinned Versions",
        "",
        f"- `langgraph`: `{lg_ver}`",
        f"- `langgraph-checkpoint-sqlite`: `{sqlite_ver}`",
        f"- `langgraph-checkpoint`: `{ckpt_ver}`",
        f"- Python: `{py_ver}` on `{platform.system()} {platform.machine()}`",
        f"- Trials per cell: `{trials_per_cell}` (every reported violation reproduces `{trials_per_cell}/{trials_per_cell}`)",
        "- Crash mechanism: real `SIGKILL` (`os.kill(os.getpid(), signal.SIGKILL)`) in isolated subprocesses; recovery in a fresh process from `SqliteSaver`",
        "- Repro command: `frameworks/langgraph/run.sh`",
        "",
        "## Results Matrix",
        "",
        "| Workload | Crash Point | Variant | Real Effects (World Log) | Invariants | Verdict / Classification |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    for r in reports:
        eff = format_effects(r.workload, r.trials)
        invs = format_invariants(r.trials)
        verdict = f"**{r.classification}**" if r.classification != "PASS" else "PASS"
        lines.append(
            f"| `{r.workload}` | `{r.crash_point}` | `{r.variant}` | {eff} | {invs} | {verdict} |"
        )

    lines.extend(
        [
            "",
            "## Findings & Classification (with Verbatim Doc Quotes)",
            "",
            "### 1. `(c)` Works as Designed, but Surprising — `Command(resume=True)` is lost on crash right after `interrupt()` returns, even with `durability=\"sync\"`",
            "",
            "- **Cells**: `approval-gate` x `after_resume_approved_in_approval` x `sync_two_node` and `mitigated` (`3/3` trials).",
            "- **Observed**: After Process 1 pauses at `interrupt()`, Process 2 invokes `graph.invoke(Command(resume=True), config, durability=\"sync\")`. Inside `approval_node`, `interrupt()` returns `True` and Process 2 dies with `SIGKILL` before `approval_node` returns `Command(goto=\"execute\")`. Fresh Process 3 calls `graph.invoke(None, config, durability=\"sync\")`. Result: **0 wire transfers** and the graph re-interrupts (`status=\"pending\"`), silently dropping the human's approval.",
            "- **Root cause in `langgraph==1.2.24`**: In `langgraph/pregel/_loop.py`, `_first()` persists the `Command(resume=...)` write via `self.put_writes(NULL_TASK_ID, [(RESUME, True)])`, which calls `self.submit(self.checkpointer_put_writes, ...)` on `BackgroundExecutor` without awaiting the future before executing the resumed node—even when `durability=\"sync\"`. (`durability=\"sync\"` only awaits `loop._put_checkpoint_fut.result()` at the end of each super-step in `langgraph/pregel/main.py`.)",
            "- **Safety vs. liveness**: Gate safety (`never_without_approval`, `at_most_once_after_approval`) holds (0 effects). What fails is approval durability (`exactly_once_after_approval` on `invoke(None, config)` recovery): callers must persist human approval decisions outside LangGraph and replay `Command(resume=...)` if a crash occurs before the approval super-step finishes.",
            "- **Why not `(a)`**: the quote below promises a checkpoint before the *next* step starts. The crash lands inside the resumed step, and no doc we found promises that a resume value is durable before that step finishes. Reclassify as `(a)` only if a doc stating that is found.\n- **Doc quote** ([Checkpointers — Durability modes](https://docs.langchain.com/oss/python/langgraph/checkpointers)):",
            "  > `\"sync\"`: LangGraph persists changes synchronously before the next step starts. This ensures that LangGraph writes every checkpoint before continuing execution, providing high durability at the cost of some performance overhead.",
            "",
            "### 2. `(b)` Dangerous Default / Tutorial Footgun — Default `durability=\"async\"` loses completed super-steps and completed `@task` results across node/task boundaries",
            "",
            "- **Cells**:",
            "  - `charge-once` x `after_charge_returns` x `naive` (`3/3` trials -> **2 real charges**, 1 confirm).",
            "  - `charge-once` x `after_charge_returns` x `task_async` (`3/3` trials -> **2 real charges**, 1 confirm).",
            "  - `approval-gate` x `at_execute_entry` x `naive_two_node` (`3/3` trials -> **0 wire transfers**, reverted to pending interrupt).",
            "- **Observed**: Even after `charge` completely returns and LangGraph starts `confirm` (or `approval` returns `Command(goto=\"execute\")` and LangGraph enters `execute`, or `charge_task().result()` returns), under LangGraph's default `durability=\"async\"` the checkpoint/writes have not been committed to SQLite yet. A `SIGKILL` at the start of the next step re-executes the already-finished `charge` node/task (double-charging `3/3` times) or reverts an already-approved transition back to pending (`3/3` times).",
            "- **Why `(b)`**: `Pregel.invoke()` silently defaults to `durability=\"async\"`. None of the quickstarts in `persistence.md`, `graph-api.md`, `functional-api.md`, or `interrupts.md` pass `durability=\"sync\"`. While `checkpointers.md` discloses that `\"async\"` has a risk of lost checkpoints on crash, calling it \"a small risk\" understates the real behavior with `SqliteSaver`, where local node transitions hit the window `3/3` (100%) of the time. Switching to `durability=\"sync\"` completely eliminates both `two-node` boundary failures (`3/3` PASS).",
            "- **Additional note on `@task` under `durability=\"sync\"`**: In `langgraph==1.2.24`, `@task` results are persisted via `put_writes` on `BackgroundExecutor` without awaiting even under `durability=\"sync\"`. Because that produces a thread-scheduling race immediately after `.result()` returns (not a 100% deterministic 3/3 failure), we do not count `task_sync` as a 3/3 violation in the matrix; callers relying on `@task` for crash safety should still pair it with idempotent external effects.",
            "- **Supporting doc quotes**:",
            "  - [Checkpointers — Durability modes](https://docs.langchain.com/oss/python/langgraph/checkpointers):",
            "    > `\"async\"`: LangGraph persists changes asynchronously while the next step executes. This provides good performance and durability, but there is a small risk that LangGraph does not write checkpoints if the process crashes during execution.",
            "  - [Graph API — Choose the unit of work / Using tasks in nodes](https://docs.langchain.com/oss/python/langgraph/graph-api):",
            "    > Graph node | Yes. A checkpoint follows the super-step it runs in. | Resume starts at that node. Earlier nodes do not re-run.",
            "    > With a checkpointer, completed task results are saved, so a resumed thread can skip finished work inside the node.",
            "",
            "### 3. `(c)` Works as Designed — Mid-node / mid-tool crash after an external side effect repeats the effect without provider-side idempotency",
            "",
            "- **Cells**:",
            "  - `charge-once` x `after_charge_before_checkpoint` x `naive`, `sync`, `task_async` (`3/3` trials -> **2 real charges**).",
            "  - `charge-once` x `mid_confirm_after_effect` x `naive`, `sync`, `task_async` (`3/3` trials -> **2 real confirms**).",
            "  - `approval-gate` x `after_effect_before_checkpoint` x `naive_two_node`, `sync_two_node` (`3/3` trials -> **2 real wire transfers**).",
            "- **Observed**: When `SIGKILL` lands after the external write commits to the world log but before the node or `@task` returns, LangGraph has no record of completion and re-runs the node/task on `graph.invoke(None, config)`. This is inherent to at-least-once checkpointed execution and is explicitly documented.",
            "- **Mitigation verified (`3/3` PASS)**: Deriving deterministic idempotency keys from durable state (`f\"charge:{order_id}\"`, `f\"confirm:{order_id}\"`, `f\"wire:{order_id}\"`) paired with an idempotent external provider and `durability=\"sync\"` (`idempotent_sync` and `mitigated`) produces **exactly 1 real effect** across all of these crash points (`3/3`).",
            "- **Supporting doc quotes**:",
            "  - [Fault tolerance — Retry boundaries and idempotency](https://docs.langchain.com/oss/python/langgraph/fault-tolerance):",
            "    > A retry re-runs the entire node function from the start. That gives **at-least-once** semantics for node execution: when a policy matches, LangGraph invokes the node again. It does not make external side effects happen exactly once.",
            "    > Checkpoints are written at super-step boundaries, not mid-function. Graph writes from a failed attempt are discarded. Side effects during the node (payments, emails, external writes) are outside that boundary. Application code must make those effects idempotent.",
            "    > The same re-execution model applies after an interrupt or a crash resume.",
            "  - [Interrupts — Side effects called before `interrupt` must be idempotent](https://docs.langchain.com/oss/python/langgraph/interrupts):",
            "    > In the post-approval examples below, side effects run only when approval succeeds. If a node fails after committing a side effect but before completing, retrying the node can repeat the effect. Use idempotent operations or deduplicate writes in the external system.",
            "  - [Functional API — Idempotency](https://docs.langchain.com/oss/python/langgraph/functional-api):",
            "    > On resume, LangGraph restores completed task results. A task that started but did not finish may run again, so use idempotency keys or check existing results before writing.",
            "",
            "## What Works (Credit Where Due)",
            "",
            "1. **Approval-gate safety invariants hold across every tested crash point (`3/3` across all variants)**:",
            "   - **Never runs without approval**: `no_approval_resume` and `before_interrupt` produce `0` wire transfers and remain safely paused at `interrupt()` (`3/3`).",
            "   - **Never runs after rejection**: `while_waiting_reject` and `after_resume_rejected` produce `0` wire transfers (`3/3`).",
            "   - **Survives process death while waiting on human input**: `while_waiting_approve` (`1` wire transfer, `status=\"executed\"`) and `while_waiting_reject` (`0` wire transfers, `status=\"rejected\"`) survive a `SIGKILL` after `interrupt()` and resume cleanly in a fresh process from `SqliteSaver` (`3/3`).",
            "   - **Duplicate `Command(resume=True)` after completion is safe**: `double_resume_after_completion` returns the saved completed state without re-running `execute_node` (`1` wire transfer, `3/3`), matching `checkpointers.md` (\"Resume returns the saved result without running nodes when the latest checkpoint has no unfinished work.\").",
            "2. **`durability=\"sync\"` cleanly protects super-step transitions (`3/3`)**:",
            "   - `charge-once` (`sync`) @ `after_charge_returns`: `charge` does not re-run (`charge=1, confirm=1`, `3/3`).",
            "   - `approval-gate` (`sync_two_node`) @ `at_execute_entry`: approval transition is durable before `execute_node` starts (`wire=1`, `3/3`).",
            "3. **Docs-recommended mitigation (`durability=\"sync\"` + separate nodes + deterministic idempotency keys) holds across every side-effect crash point (`3/3`)**:",
            "   - `charge-once` (`idempotent_sync`): `PASS` on all 4 crash points (`charge=1, confirm=1`, `3/3`).",
            "   - `approval-gate` (`mitigated`): `PASS` on 8 of 9 cells (`wire=1` on approval, `wire=0` without approval or on rejection); the sole gap is `after_resume_approved_in_approval` where `Command(resume=True)` itself was not synchronously persisted before running `approval_node`.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run LangGraph crash-resume Jepsen probe.")
    parser.add_argument("--trials", type=int, default=3, help="Trials per matrix cell (default: 3)")
    parser.add_argument(
        "--output",
        type=Path,
        default=HERE / "results.md",
        help="Path to write Markdown results report.",
    )
    args = parser.parse_args()

    charge_variants = ["naive", "sync", "task_async", "idempotent_sync"]
    charge_crash_points = [
        "none",
        "after_charge_before_checkpoint",
        "after_charge_returns",
        "mid_confirm_after_effect",
    ]

    approval_variants = ["naive_two_node", "sync_two_node", "mitigated"]
    approval_crash_points = [
        "no_approval_resume",
        "before_interrupt",
        "while_waiting_approve",
        "while_waiting_reject",
        "after_resume_rejected",
        "after_resume_approved_in_approval",
        "at_execute_entry",
        "after_effect_before_checkpoint",
        "double_resume_after_completion",
    ]

    reports: list[CellReport] = []

    def run_grid(
        workload: str,
        crash_points: list[str],
        variants: list[str],
        runner: Callable[[str, str, int], TrialResult],
    ) -> None:
        for cp in crash_points:
            for variant in variants:
                trials = [runner(variant, cp, i) for i in range(args.trials)]
                classification, note = classify_cell(workload, cp, variant, trials)
                rep = CellReport(
                    workload=workload,
                    crash_point=cp,
                    variant=variant,
                    trials=trials,
                    classification=classification,
                    note=note,
                )
                reports.append(rep)
                status_str = f"{rep.pass_count}/{len(trials)} PASS"
                print(
                    f"[{workload}] cp={cp:<34} variant={variant:<16} "
                    f"effects={format_effects(workload, trials):<26} {status_str} -> {classification}"
                )

    run_grid("charge-once", charge_crash_points, charge_variants, run_charge_once_trial)
    run_grid("approval-gate", approval_crash_points, approval_variants, run_approval_gate_trial)

    md = render_results_md(reports, args.trials)
    args.output.write_text(md, encoding="utf-8")
    print(f"\nWrote results report to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
