# Place-order saga verification

TLC checks the protocol in [SagaProtocol.tla](SagaProtocol.tla). Each action names its implementation
function and line range. The state separates durable rows, external effects, and process-local
execution. A crash drops the local state and recovery uses the persisted saga and intents.

## Run

```sh
verification/tla/run.sh
PYTHONPATH=src python verification/tla/replay.py
```

The runner uses one breadth-first worker with a fixed fingerprint polynomial and seed. It requires
Bash, ripgrep, `sha256sum`, and Java. The jar is **TLC 2026.11.19.180144** (TLA+ Tools 2.0,
2026-11-19, revision `bc1b506bdcad80bb518bf3776a9161c34ff0fc4c`). Use the pinned
`~/.cache/tla/tla2tools.jar` or set `TLA2TOOLS_JAR` to a copy. The runner checks SHA-256
`fdffef247f563ef6edbfd44958d2a82db79e7564e07cb673262d896353fa6713`. The recorded run used
Temurin Java 21.0.5. The deterministic simulator replays require Python 3.12+ and ran on
Python 3.14.7.

The runner prints the directory containing all logs and succeeds when the complete matrix reproduces
the expected outcomes: the fixed runtime passes and every mutant fails. Pass a config name to get
that config's full TLC trace and raw exit code, for example `verification/tla/run.sh doubt-mutant`
exits 12. TLC uses exit 13 for a liveness violation.

## Properties

| Property | Result |
| --- | --- |
| **At most one real payment effect per step.** A lost response must never cause a duplicate charge or refund. | **Pass** for stable keys in `fixed`. **Fail** for fresh keys in `toy`. |
| **An email requires earlier durable approval for that exact action.** A rejected or unanswered request must never send. | **Pass** in `fixed`. The removed gate check fails after a recorded rejection. |
| **Every submitted order reaches commit, compensation, or escalation.** Escalation must be recorded in the audit. | **Pass** in `fixed`, including a crash before the first durable write, because the client resubmits until it has an ack. Fails if the ack comes before the write. Reordering the escalation writes breaks termination with audited escalation for a persisted saga. |
| **A compensated saga has no irreversible effect.** A sent email cannot be undone. | **Pass** in `fixed`: a crash-interrupted intent is in doubt, so an email that may have landed escalates. Fails without the in-doubt marking, without writing a re-called failed intent back to started, and in the deliberate post-pivot compensation variant. |

### Runtime counterexamples

The first two were found by this model in the runtime at `88e4e3d`. A third came from the model
after the first fix. All are fixed, and each has a regression test that replays it through
`ExplicitChoices` (`tests/test_recovery.py`, `tests/test_submission.py`, `verification/tla/replay.py`).

- **Duplicate charge in `toy`.** Reserve inventory and persist the payment intent. The charge using
  `saga-17:charge` lands and the response times out. `_call_step()` writes
  `saga-17:charge:r1` into the intent and the retry charges again. The same explicit choice in
  `fixed` reuses the original key and produces one charge. This stays a live counterexample: `toy`
  is the planted bug.
- **Lost submission.** Crash just before the initial `put_saga()` in `run()`. The simulator
  restarts and calls `recover()`. `open_sagas()` finds no row and nothing replays the submission,
  and the old termination oracle passed vacuously because it only checked persisted sagas.
  Fix: the client records the order, treats it as accepted only on `submit()`'s ack, and
  resubmits when none arrives. `submit()` is idempotent by order id, and `saga_terminates` checks
  every order the client submitted. TLC: `submission` passes; `ack-mutant` fails.
- **Compensation after delivery.** Reserve and charge, record approval, and persist the email
  intent. The email lands and the process dies inside `Host.call()` or before `_mark()`.
  `recover()` replays the pending email with the stable key. Four rate limits exhaust the
  attempts and left `Intent.unknown` false, so `_handle()` compensated and the saga ended
  `compensated` with the email delivered. Fix: `recover()` first records every intent still
  `started` as in doubt (`_mark_in_doubt`), so the exhausted email escalates, and `_compensate`
  escalates instead of undoing anything once the email may have been sent. A charge that landed
  before the crash is now probed and refunded instead of treated as rejected. TLC: `compensation`
  passes; `doubt-mutant` fails.
- **Stale failure, found after the fix.** Four rate limits fail the email intent and the process
  dies just after that write. The replay sends the email and dies again before seeing the answer.
  `_call_step` had called the failed intent again without leaving `failed`, so the next replay's
  four rate limits again read as a definite failure and compensated a delivered email. Fix: a
  failed intent that is called again is written back to `started` first. TLC: `retry-mutant`
  fails; `pivot-guard` found it first, by failing while the guard held.

The timeouts, crashes, and rate limits in these paths are documented faults in the simulator,
except bursts of rate limits and 5xx errors, which are tagged `theoretical`. A plain seeded sweep
(`shape run --seeds 10000 --saga fixed`) found the compensation finding on the unfixed runtime
(13 failing seeds) because bursts persist across the retry budget and crashes inside tool calls
are likelier. It also found the lost submission (56 failing seeds) once the oracle checked the
client's own record. The stale-failure path needs two crashes and two bursts and is only reached
by targeted replay.

### Negative controls

- **`approval-mutant`** ignores the result of `_gate()`. A human rejects, the rejection is written,
  and email sends without a positive audit entry.
- **`pivot-mutant`** makes `_handle()` transition to `compensating` after email succeeds and has no
  guard in `_compensate`. The charge and reservation are undone, and the email remains delivered.
  **`pivot-guard`** is the same handler mutation with the guard in `_compensate`, and it passes:
  the guard alone keeps a delivered email from being compensated.
- **`doubt-mutant`** skips `_mark_in_doubt` in `recover()`. The email lands, a crash cuts off the
  mark, and the exhausted replay compensates.
- **`retry-mutant`** calls a failed intent again without writing it back to `started`.
- **`ack-mutant`** acks the client before the first durable write. A crash in between loses the
  order and the client never resubmits.
- **`order-mutant`** moves the escalation audit after `_settle(ESCALATED)`. A release fails, the
  saga's escalated row lands, and a crash cuts off the escalation audit. On restart
  `open_sagas()` filters out the terminal row, leaving an escalation with no escalation entry.

Earlier each variant was also replayed with the corresponding scratch edit in `runtime.py`. The
approval check's `replay.py approval` exited 1 on the scratch edit with “email.send happened
without durable approval for its exact action.” The runtime mutants in `tests/test_recovery.py`
and `tests/test_invariants.py` play the same role for the in-doubt, guard, and ack controls.

## Model bounds and assumptions

- One order, submitted by one client that persists across crashes and records its ack in the
  world. It resubmits only when idle without an ack. One runtime driver at a time, one inventory record with enough initial
  stock, one charge step, one refund step, one email action. Charge and refund effects are counted
  separately per step. The policy can propose valid steps, duplicates, out-of-order actions,
  invalid actions, or a stop. The durable cursor allows at most 12 decisions.
- At most three process crashes across all modeled write and tool-call boundaries, with a recovery
  after each crash. There are up to four tries per call loop invocation and up to three CAS
  rounds per reserve or release invocation. Local counters restart where the Python code does.
  A successful call includes the effect and its fake API idempotency entry in one transition.
  Tool effects and results written to the store are separate transitions. Crash-before and
  crash-after store writes are available even for audit rows whose contents the model erases.
- Pre-commit timeout, lost response after commit, rate limit, transient server error, and
  compensation rejection are explored. Errors that induce the same next state share a branch.
  The simulator's burst faults are runs of those same errors, so a bounded run of them is already
  covered. The model does not weight them.
  Duplicate delivery is equivalent to one execution here because of key deduplication and CAS.
  A successful reserve changes the hold used by CAS. A lost response can make a retry of the same
  CAS stale. The model does not use clock time or host fault weights. Ignoring crash reservations
  that never raise leaves three actual crashes available at any applicable boundary.
- The simulated answer is fixed for a request. A 30-second delay is equivalent to approval
  under the current 60-second timeout. Rejection and a timeout from a silent approver are explored.
  The audit stores saga ID, request ID, and answer. The model binds a positive entry to the
  exact email by the saga's immutable order and deterministic arguments. The runtime separately
  rejects proposed arguments that differ from those arguments.
- Liveness uses weak fairness of `Next`. The driver or supervisor cannot stop taking available
  steps indefinitely. Retry budgets may be exhausted with every attempt failing. A post-crash
  scan can finish with no work if the first saga row was never persisted, and the client then
  resubmits.
- The model excludes concurrent sagas or drivers, outside inventory edits, other order fixtures and
  tools, mutable approval requests, real time scheduling and cancellation, provider key expiry,
  corrupted storage, and the terminal-state `compensate_again()` diagnostic. It uses the
  in-repo fake API contracts. It does not check amount, inventory, refund reconciliation, or audit
  completeness beyond the four stated rules.

## TLC run record

The complete pass covers the entire graph for the listed bounds. For each failing run the counts
are the states TLC had reached when it reported a counterexample. For liveness failures that count
can vary with the timing of TLC’s periodic checks. The full matrix took 46.8 seconds wall time.
Durations in the table are TLC’s displayed resolution. `submission` and `compensation` check
different properties over the same graph as `fixed`.

| Config | Outcome | Generated | Distinct | TLC time |
| --- | --- | ---: | ---: | ---: |
| `fixed` | Payment, approval, clean compensation, and submission termination hold | 562,764 | 214,785 | 11 s |
| `submission` | Every submitted order terminates | 562,764 | 214,785 | 11 s |
| `compensation` | No compensation after delivery | 562,764 | 214,785 | 3 s |
| `pivot-guard` | Guard blocks compensation after a post-pivot handler mutation | 563,584 | 215,245 | 3 s |
| `toy` | Duplicate charge | 5,915 | 2,613 | <1 s |
| `approval-mutant` | Email with no approval | 19,167 | 8,207 | <1 s |
| `pivot-mutant` | Compensation after successful delivery | 118,865 | 48,450 | 1 s |
| `doubt-mutant` | Compensation after delivery and a crash | 367,905 | 143,586 | 2 s |
| `retry-mutant` | Compensation after a stale failure | 515,359 | 197,969 | 3 s |
| `order-mutant` | Escalation loses its audit | 157,095 | 63,795 | 3 s |
| `ack-mutant` | Submission lost before its first write | 153,353 | 58,583 | 3 s |

Pstack's `/statechart` rendered the original counterexamples using its bundled TLC 1.7.4. Its XML
reader could not read the pinned November 2026 jar. The run counts above are from the pinned
runner. Statechart groups repeating control states and draws the failing paths only for a graph
this size.
