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
the expected outcomes, including the fixed-runtime violations. Pass a config name to get that
config's full TLC trace and raw exit code, for example `verification/tla/run.sh compensation` exits
12. TLC uses exit 13 for a liveness violation.

## Properties

| Property | Result |
| --- | --- |
| **At most one real payment effect per step.** A lost response must never cause a duplicate charge or refund. | **Pass** for stable keys in `fixed`. **Fail** for fresh keys in `toy`. |
| **An email requires earlier durable approval for that exact action.** A rejected or unanswered request must never send. | **Pass** in `fixed`. The removed gate check fails after a recorded rejection. |
| **Every saga reaches commit, compensation, or escalation.** Escalation must be recorded in the audit. | **Pass** in `fixed` for sagas with a durable row. At the submission boundary, **fail** when a crash prevents the initial row from landing. Reordering writes also breaks termination with audited escalation for a saga that was already persisted. |
| **A compensated saga has no irreversible effect.** A sent email cannot be undone. | **Fail** in `fixed`. The deliberate post-pivot compensation variant also fails, with a successful send and no crash. |

### Runtime counterexamples

- **Duplicate charge in `toy`.** Reserve inventory and persist the payment intent. The charge using
  `saga-17:charge` lands and the response times out. `_call_step()` writes
  `saga-17:charge:r1` into the intent and the retry charges again. The same explicit choice in
  `fixed` reuses the original key and produces one charge.
- **Lost submission in `fixed`.** Crash just before the initial `put_saga()` in `run()`.
  The simulator restarts and calls `recover()`. `open_sagas()` finds no row and nothing replays
  the submission. The simulator's termination oracle passes vacuously because it only checks
  persisted sagas. If the caller defines admission as the initial durable write, liveness holds
  from that boundary.
- **Compensation after delivery in `fixed`.** Reserve and charge, record approval, and persist
  the email intent. The email lands and the process dies inside `Host.call()` before `_mark()`.
  `recover()` replays the pending email with the stable key. Four pre-execution rate limits
  exhaust the attempts and leave `Intent.unknown` false. `_handle()` enters compensation,
  the runtime refunds the charge and releases inventory, and the saga ends `compensated` with
  the email still delivered. The simulator reports `rollback_restores_state`.

All three paths were replayed through `ExplicitChoices`. The timeouts, crashes, and rate limits in
those paths are classified as documented faults by the simulator. The fixed-saga seed sweep for
seeds 0–999 had zero failures. The two fixed counterexamples came from targeted replay.

### Negative controls

- **`approval-mutant`** ignores the result of `_gate()`. A human rejects, the rejection is written,
  and email sends without a positive audit entry.
- **`pivot-mutant`** makes `_handle()` transition to `compensating` after email succeeds. The
  charge and reservation are undone, and the email remains delivered.
- **`order-mutant`** moves the escalation audit after `_settle(ESCALATED)`. A release fails, the
  saga's escalated row lands, and a crash cuts off the escalation audit. On restart
  `open_sagas()` filters out the terminal row, leaving an escalation with no escalation entry.

Each variant was also replayed with the corresponding scratch edit in `runtime.py`. The generated
skill's source comparison exposed the removed gate condition. Its `replay.py approval` check
exited 1 on the scratch edit with “email.send happened without durable approval for its exact
action.” The matching TLC mutant exited 12. The file was restored byte for byte, and the
unmodified-source replays and TLC matrix were rerun.

## Model bounds and assumptions

- One submitted order, one runtime driver at a time, one inventory record with enough initial
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
  scan can finish with no work if the first saga row was never persisted.
- The model excludes concurrent sagas or drivers, outside inventory edits, other order fixtures and
  tools, mutable approval requests, real time scheduling and cancellation, provider key expiry,
  corrupted storage, and the terminal-state `compensate_again()` diagnostic. It uses the
  in-repo fake API contracts. It does not check amount, inventory, refund reconciliation, or audit
  completeness beyond the four stated rules.

## TLC run record

The complete pass covers the entire graph for the listed bounds. For each failing run the counts
are the states TLC had reached when it reported a counterexample. For liveness failures that count
can vary with the timing of TLC’s periodic checks. The full matrix took 19.68 seconds wall time.
Durations in the table are TLC’s displayed resolution.

| Config | Outcome | Generated | Distinct | TLC time |
| --- | --- | ---: | ---: | ---: |
| `fixed` | Payment, approval, and recorded-saga liveness hold | 435,733 | 153,828 | 8 s |
| `toy` | Duplicate charge | 3,749 | 1,521 | <1 s |
| `approval-mutant` | Email with no approval | 11,875 | 4,741 | <1 s |
| `pivot-mutant` | Compensation after successful delivery | 78,299 | 29,695 | 1 s |
| `order-mutant` | Escalation loses its audit | 167,607 | 62,948 | 3 s |
| `submission` | Submission lost before its first write | 175,769 | 64,690 | 3 s |
| `compensation` | Compensation after delivery and a crash | 207,262 | 75,686 | 1 s |

Pstack's `/statechart` rendered the counterexamples using its bundled TLC 1.7.4. Its XML reader
could not read the pinned November 2026 jar. The run counts above are from the pinned runner.
Statechart groups repeating control states and draws the failing paths only for a graph this size.
