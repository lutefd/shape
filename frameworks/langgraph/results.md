# LangGraph Crash-Resume Probe Results

## Environment & Pinned Versions

- `langgraph`: `1.2.24`
- `langgraph-checkpoint-sqlite`: `3.1.3`
- `langgraph-checkpoint`: `4.3.2`
- Python: `3.14.7` on `Linux x86_64`
- Trials per cell: `3` (every reported violation reproduces `3/3`)
- Crash mechanism: real `SIGKILL` (`os.kill(os.getpid(), signal.SIGKILL)`) in isolated subprocesses; recovery in a fresh process from `SqliteSaver`
- Repro command: `frameworks/langgraph/run.sh`

## Results Matrix

| Workload | Crash Point | Variant | Real Effects (World Log) | Invariants | Verdict / Classification |
| --- | --- | --- | --- | --- | --- |
| `charge-once` | `none` | `naive` | charge=1, confirm=1 | `at_most_once_charge`: PASS (3/3); `at_most_once_confirm`: PASS (3/3); `completed_with_effects`: PASS (3/3) | PASS |
| `charge-once` | `none` | `sync` | charge=1, confirm=1 | `at_most_once_charge`: PASS (3/3); `at_most_once_confirm`: PASS (3/3); `completed_with_effects`: PASS (3/3) | PASS |
| `charge-once` | `none` | `task_async` | charge=1, confirm=1 | `at_most_once_charge`: PASS (3/3); `at_most_once_confirm`: PASS (3/3); `completed_with_effects`: PASS (3/3) | PASS |
| `charge-once` | `none` | `idempotent_sync` | charge=1, confirm=1 | `at_most_once_charge`: PASS (3/3); `at_most_once_confirm`: PASS (3/3); `completed_with_effects`: PASS (3/3) | PASS |
| `charge-once` | `after_charge_before_checkpoint` | `naive` | charge=2, confirm=1 | `at_most_once_charge`: FAIL (0/3); `at_most_once_confirm`: PASS (3/3); `completed_with_effects`: FAIL (0/3) | **(c) works as designed** |
| `charge-once` | `after_charge_before_checkpoint` | `sync` | charge=2, confirm=1 | `at_most_once_charge`: FAIL (0/3); `at_most_once_confirm`: PASS (3/3); `completed_with_effects`: FAIL (0/3) | **(c) works as designed** |
| `charge-once` | `after_charge_before_checkpoint` | `task_async` | charge=2, confirm=1 | `at_most_once_charge`: FAIL (0/3); `at_most_once_confirm`: PASS (3/3); `completed_with_effects`: FAIL (0/3) | **(c) works as designed** |
| `charge-once` | `after_charge_before_checkpoint` | `idempotent_sync` | charge=1, confirm=1 | `at_most_once_charge`: PASS (3/3); `at_most_once_confirm`: PASS (3/3); `completed_with_effects`: PASS (3/3) | PASS |
| `charge-once` | `after_charge_returns` | `naive` | charge=2, confirm=1 | `at_most_once_charge`: FAIL (0/3); `at_most_once_confirm`: PASS (3/3); `completed_with_effects`: FAIL (0/3) | **(b) dangerous default / tutorial footgun** |
| `charge-once` | `after_charge_returns` | `sync` | charge=1, confirm=1 | `at_most_once_charge`: PASS (3/3); `at_most_once_confirm`: PASS (3/3); `completed_with_effects`: PASS (3/3) | PASS |
| `charge-once` | `after_charge_returns` | `task_async` | charge=2, confirm=1 | `at_most_once_charge`: FAIL (0/3); `at_most_once_confirm`: PASS (3/3); `completed_with_effects`: FAIL (0/3) | **(b) dangerous default / tutorial footgun** |
| `charge-once` | `after_charge_returns` | `idempotent_sync` | charge=1, confirm=1 | `at_most_once_charge`: PASS (3/3); `at_most_once_confirm`: PASS (3/3); `completed_with_effects`: PASS (3/3) | PASS |
| `charge-once` | `mid_confirm_after_effect` | `naive` | charge=1, confirm=2 | `at_most_once_charge`: PASS (3/3); `at_most_once_confirm`: FAIL (0/3); `completed_with_effects`: FAIL (0/3) | **(c) works as designed** |
| `charge-once` | `mid_confirm_after_effect` | `sync` | charge=1, confirm=2 | `at_most_once_charge`: PASS (3/3); `at_most_once_confirm`: FAIL (0/3); `completed_with_effects`: FAIL (0/3) | **(c) works as designed** |
| `charge-once` | `mid_confirm_after_effect` | `task_async` | charge=1, confirm=2 | `at_most_once_charge`: PASS (3/3); `at_most_once_confirm`: FAIL (0/3); `completed_with_effects`: FAIL (0/3) | **(c) works as designed** |
| `charge-once` | `mid_confirm_after_effect` | `idempotent_sync` | charge=1, confirm=1 | `at_most_once_charge`: PASS (3/3); `at_most_once_confirm`: PASS (3/3); `completed_with_effects`: PASS (3/3) | PASS |
| `approval-gate` | `no_approval_resume` | `naive_two_node` | wire=0 (interrupted) | `never_without_approval`: PASS (3/3); `remains_interrupted`: PASS (3/3) | PASS |
| `approval-gate` | `no_approval_resume` | `sync_two_node` | wire=0 (interrupted) | `never_without_approval`: PASS (3/3); `remains_interrupted`: PASS (3/3) | PASS |
| `approval-gate` | `no_approval_resume` | `mitigated` | wire=0 (interrupted) | `never_without_approval`: PASS (3/3); `remains_interrupted`: PASS (3/3) | PASS |
| `approval-gate` | `before_interrupt` | `naive_two_node` | wire=0 (interrupted) | `never_without_approval`: PASS (3/3); `remains_interrupted`: PASS (3/3) | PASS |
| `approval-gate` | `before_interrupt` | `sync_two_node` | wire=0 (interrupted) | `never_without_approval`: PASS (3/3); `remains_interrupted`: PASS (3/3) | PASS |
| `approval-gate` | `before_interrupt` | `mitigated` | wire=0 (interrupted) | `never_without_approval`: PASS (3/3); `remains_interrupted`: PASS (3/3) | PASS |
| `approval-gate` | `while_waiting_approve` | `naive_two_node` | wire=1 (completed) | `at_most_once_after_approval`: PASS (3/3); `exactly_once_after_approval`: PASS (3/3) | PASS |
| `approval-gate` | `while_waiting_approve` | `sync_two_node` | wire=1 (completed) | `at_most_once_after_approval`: PASS (3/3); `exactly_once_after_approval`: PASS (3/3) | PASS |
| `approval-gate` | `while_waiting_approve` | `mitigated` | wire=1 (completed) | `at_most_once_after_approval`: PASS (3/3); `exactly_once_after_approval`: PASS (3/3) | PASS |
| `approval-gate` | `while_waiting_reject` | `naive_two_node` | wire=0 (completed) | `never_after_rejection`: PASS (3/3); `reached_rejected`: PASS (3/3) | PASS |
| `approval-gate` | `while_waiting_reject` | `sync_two_node` | wire=0 (completed) | `never_after_rejection`: PASS (3/3); `reached_rejected`: PASS (3/3) | PASS |
| `approval-gate` | `while_waiting_reject` | `mitigated` | wire=0 (completed) | `never_after_rejection`: PASS (3/3); `reached_rejected`: PASS (3/3) | PASS |
| `approval-gate` | `after_resume_rejected` | `naive_two_node` | wire=0 (interrupted) | `never_after_rejection`: PASS (3/3); `no_unauthorized_execution`: PASS (3/3) | PASS |
| `approval-gate` | `after_resume_rejected` | `sync_two_node` | wire=0 (interrupted) | `never_after_rejection`: PASS (3/3); `no_unauthorized_execution`: PASS (3/3) | PASS |
| `approval-gate` | `after_resume_rejected` | `mitigated` | wire=0 (interrupted) | `never_after_rejection`: PASS (3/3); `no_unauthorized_execution`: PASS (3/3) | PASS |
| `approval-gate` | `after_resume_approved_in_approval` | `naive_two_node` | wire=0 (interrupted) | `at_most_once_after_approval`: PASS (3/3); `exactly_once_after_approval`: FAIL (0/3) | **(b) dangerous default / tutorial footgun** |
| `approval-gate` | `after_resume_approved_in_approval` | `sync_two_node` | wire=0 (interrupted) | `at_most_once_after_approval`: PASS (3/3); `exactly_once_after_approval`: FAIL (0/3) | **(c) works as designed** |
| `approval-gate` | `after_resume_approved_in_approval` | `mitigated` | wire=0 (interrupted) | `at_most_once_after_approval`: PASS (3/3); `exactly_once_after_approval`: FAIL (0/3) | **(c) works as designed** |
| `approval-gate` | `at_execute_entry` | `naive_two_node` | wire=0 (interrupted) | `at_most_once_after_approval`: PASS (3/3); `exactly_once_after_approval`: FAIL (0/3) | **(b) dangerous default / tutorial footgun** |
| `approval-gate` | `at_execute_entry` | `sync_two_node` | wire=1 (completed) | `at_most_once_after_approval`: PASS (3/3); `exactly_once_after_approval`: PASS (3/3) | PASS |
| `approval-gate` | `at_execute_entry` | `mitigated` | wire=1 (completed) | `at_most_once_after_approval`: PASS (3/3); `exactly_once_after_approval`: PASS (3/3) | PASS |
| `approval-gate` | `after_effect_before_checkpoint` | `naive_two_node` | wire=2 (completed) | `at_most_once_after_approval`: FAIL (0/3); `exactly_once_after_approval`: FAIL (0/3) | **(c) works as designed** |
| `approval-gate` | `after_effect_before_checkpoint` | `sync_two_node` | wire=2 (completed) | `at_most_once_after_approval`: FAIL (0/3); `exactly_once_after_approval`: FAIL (0/3) | **(c) works as designed** |
| `approval-gate` | `after_effect_before_checkpoint` | `mitigated` | wire=1 (completed) | `at_most_once_after_approval`: PASS (3/3); `exactly_once_after_approval`: PASS (3/3) | PASS |
| `approval-gate` | `double_resume_after_completion` | `naive_two_node` | wire=1 (completed) | `at_most_once_after_approval`: PASS (3/3); `exactly_once_after_approval`: PASS (3/3) | PASS |
| `approval-gate` | `double_resume_after_completion` | `sync_two_node` | wire=1 (completed) | `at_most_once_after_approval`: PASS (3/3); `exactly_once_after_approval`: PASS (3/3) | PASS |
| `approval-gate` | `double_resume_after_completion` | `mitigated` | wire=1 (completed) | `at_most_once_after_approval`: PASS (3/3); `exactly_once_after_approval`: PASS (3/3) | PASS |

## Findings & Classification (with Verbatim Doc Quotes)

### 1. `(c)` Works as Designed, but Surprising — `Command(resume=True)` is lost on crash right after `interrupt()` returns, even with `durability="sync"`

- **Cells**: `approval-gate` x `after_resume_approved_in_approval` x `sync_two_node` and `mitigated` (`3/3` trials).
- **Observed**: After Process 1 pauses at `interrupt()`, Process 2 invokes `graph.invoke(Command(resume=True), config, durability="sync")`. Inside `approval_node`, `interrupt()` returns `True` and Process 2 dies with `SIGKILL` before `approval_node` returns `Command(goto="execute")`. Fresh Process 3 calls `graph.invoke(None, config, durability="sync")`. Result: **0 wire transfers** and the graph re-interrupts (`status="pending"`), silently dropping the human's approval.
- **Root cause in `langgraph==1.2.24`**: In `langgraph/pregel/_loop.py`, `_first()` persists the `Command(resume=...)` write via `self.put_writes(NULL_TASK_ID, [(RESUME, True)])`, which calls `self.submit(self.checkpointer_put_writes, ...)` on `BackgroundExecutor` without awaiting the future before executing the resumed node—even when `durability="sync"`. (`durability="sync"` only awaits `loop._put_checkpoint_fut.result()` at the end of each super-step in `langgraph/pregel/main.py`.)
- **Safety vs. liveness**: Gate safety (`never_without_approval`, `at_most_once_after_approval`) holds (0 effects). What fails is approval durability (`exactly_once_after_approval` on `invoke(None, config)` recovery): callers must persist human approval decisions outside LangGraph and replay `Command(resume=...)` if a crash occurs before the approval super-step finishes.
- **Why not `(a)`**: the quote below promises a checkpoint before the *next* step starts. The crash lands inside the resumed step, and no doc we found promises that a resume value is durable before that step finishes. Reclassify as `(a)` only if a doc stating that is found.
- **Doc quote** ([Checkpointers — Durability modes](https://docs.langchain.com/oss/python/langgraph/checkpointers)):
  > `"sync"`: LangGraph persists changes synchronously before the next step starts. This ensures that LangGraph writes every checkpoint before continuing execution, providing high durability at the cost of some performance overhead.

### 2. `(b)` Dangerous Default / Tutorial Footgun — Default `durability="async"` loses completed super-steps and completed `@task` results across node/task boundaries

- **Cells**:
  - `charge-once` x `after_charge_returns` x `naive` (`3/3` trials -> **2 real charges**, 1 confirm).
  - `charge-once` x `after_charge_returns` x `task_async` (`3/3` trials -> **2 real charges**, 1 confirm).
  - `approval-gate` x `at_execute_entry` x `naive_two_node` (`3/3` trials -> **0 wire transfers**, reverted to pending interrupt).
- **Observed**: Even after `charge` completely returns and LangGraph starts `confirm` (or `approval` returns `Command(goto="execute")` and LangGraph enters `execute`, or `charge_task().result()` returns), under LangGraph's default `durability="async"` the checkpoint/writes have not been committed to SQLite yet. A `SIGKILL` at the start of the next step re-executes the already-finished `charge` node/task (double-charging `3/3` times) or reverts an already-approved transition back to pending (`3/3` times).
- **Why `(b)`**: `Pregel.invoke()` silently defaults to `durability="async"`. None of the quickstarts in `persistence.md`, `graph-api.md`, `functional-api.md`, or `interrupts.md` pass `durability="sync"`. While `checkpointers.md` discloses that `"async"` has a risk of lost checkpoints on crash, calling it "a small risk" understates the real behavior with `SqliteSaver`, where local node transitions hit the window `3/3` (100%) of the time. Switching to `durability="sync"` completely eliminates both `two-node` boundary failures (`3/3` PASS).
- **Additional note on `@task` under `durability="sync"`**: In `langgraph==1.2.24`, `@task` results are persisted via `put_writes` on `BackgroundExecutor` without awaiting even under `durability="sync"`. Because that produces a thread-scheduling race immediately after `.result()` returns (not a 100% deterministic 3/3 failure), we do not count `task_sync` as a 3/3 violation in the matrix; callers relying on `@task` for crash safety should still pair it with idempotent external effects.
- **Supporting doc quotes**:
  - [Checkpointers — Durability modes](https://docs.langchain.com/oss/python/langgraph/checkpointers):
    > `"async"`: LangGraph persists changes asynchronously while the next step executes. This provides good performance and durability, but there is a small risk that LangGraph does not write checkpoints if the process crashes during execution.
  - [Graph API — Choose the unit of work / Using tasks in nodes](https://docs.langchain.com/oss/python/langgraph/graph-api):
    > Graph node | Yes. A checkpoint follows the super-step it runs in. | Resume starts at that node. Earlier nodes do not re-run.
    > With a checkpointer, completed task results are saved, so a resumed thread can skip finished work inside the node.

### 3. `(c)` Works as Designed — Mid-node / mid-tool crash after an external side effect repeats the effect without provider-side idempotency

- **Cells**:
  - `charge-once` x `after_charge_before_checkpoint` x `naive`, `sync`, `task_async` (`3/3` trials -> **2 real charges**).
  - `charge-once` x `mid_confirm_after_effect` x `naive`, `sync`, `task_async` (`3/3` trials -> **2 real confirms**).
  - `approval-gate` x `after_effect_before_checkpoint` x `naive_two_node`, `sync_two_node` (`3/3` trials -> **2 real wire transfers**).
- **Observed**: When `SIGKILL` lands after the external write commits to the world log but before the node or `@task` returns, LangGraph has no record of completion and re-runs the node/task on `graph.invoke(None, config)`. This is inherent to at-least-once checkpointed execution and is explicitly documented.
- **Mitigation verified (`3/3` PASS)**: Deriving deterministic idempotency keys from durable state (`f"charge:{order_id}"`, `f"confirm:{order_id}"`, `f"wire:{order_id}"`) paired with an idempotent external provider and `durability="sync"` (`idempotent_sync` and `mitigated`) produces **exactly 1 real effect** across all of these crash points (`3/3`).
- **Supporting doc quotes**:
  - [Fault tolerance — Retry boundaries and idempotency](https://docs.langchain.com/oss/python/langgraph/fault-tolerance):
    > A retry re-runs the entire node function from the start. That gives **at-least-once** semantics for node execution: when a policy matches, LangGraph invokes the node again. It does not make external side effects happen exactly once.
    > Checkpoints are written at super-step boundaries, not mid-function. Graph writes from a failed attempt are discarded. Side effects during the node (payments, emails, external writes) are outside that boundary. Application code must make those effects idempotent.
    > The same re-execution model applies after an interrupt or a crash resume.
  - [Interrupts — Side effects called before `interrupt` must be idempotent](https://docs.langchain.com/oss/python/langgraph/interrupts):
    > In the post-approval examples below, side effects run only when approval succeeds. If a node fails after committing a side effect but before completing, retrying the node can repeat the effect. Use idempotent operations or deduplicate writes in the external system.
  - [Functional API — Idempotency](https://docs.langchain.com/oss/python/langgraph/functional-api):
    > On resume, LangGraph restores completed task results. A task that started but did not finish may run again, so use idempotency keys or check existing results before writing.

## What Works (Credit Where Due)

1. **Approval-gate safety invariants hold across every tested crash point (`3/3` across all variants)**:
   - **Never runs without approval**: `no_approval_resume` and `before_interrupt` produce `0` wire transfers and remain safely paused at `interrupt()` (`3/3`).
   - **Never runs after rejection**: `while_waiting_reject` and `after_resume_rejected` produce `0` wire transfers (`3/3`).
   - **Survives process death while waiting on human input**: `while_waiting_approve` (`1` wire transfer, `status="executed"`) and `while_waiting_reject` (`0` wire transfers, `status="rejected"`) survive a `SIGKILL` after `interrupt()` and resume cleanly in a fresh process from `SqliteSaver` (`3/3`).
   - **Duplicate `Command(resume=True)` after completion is safe**: `double_resume_after_completion` returns the saved completed state without re-running `execute_node` (`1` wire transfer, `3/3`), matching `checkpointers.md` ("Resume returns the saved result without running nodes when the latest checkpoint has no unfinished work.").
2. **`durability="sync"` cleanly protects super-step transitions (`3/3`)**:
   - `charge-once` (`sync`) @ `after_charge_returns`: `charge` does not re-run (`charge=1, confirm=1`, `3/3`).
   - `approval-gate` (`sync_two_node`) @ `at_execute_entry`: approval transition is durable before `execute_node` starts (`wire=1`, `3/3`).
3. **Docs-recommended mitigation (`durability="sync"` + separate nodes + deterministic idempotency keys) holds across every side-effect crash point (`3/3`)**:
   - `charge-once` (`idempotent_sync`): `PASS` on all 4 crash points (`charge=1, confirm=1`, `3/3`).
   - `approval-gate` (`mitigated`): `PASS` on 8 of 9 cells (`wire=1` on approval, `wire=0` without approval or on rejection); the sole gap is `after_resume_approved_in_approval` where `Command(resume=True)` itself was not synchronously persisted before running `approval_node`.
