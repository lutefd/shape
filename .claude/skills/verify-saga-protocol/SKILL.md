---
name: verify-saga-protocol
description: Check the place-order saga's payment deduplication, durable email approval, termination, and compensation in TLA+. Use when changing Runtime or Store, including retry keys, crash recovery, pending actions, approval ordering, or compensation.
---

# Verify the saga protocol

Run from the repository root. The model and limits are in [verification/tla](../../../verification/tla/README.md).

## Coverage

Last source sync commit is `824fd3b38aa3f86ffe5d1942da200e2ab3748ea0`.

| File | Functions and contracts covered |
| --- | --- |
| `src/shape/runtime/runtime.py` | `submit`, `run`, `recover`, `_mark_in_doubt`, `_drive`, `_handle`, `_args`, `_done`, `_reject`, `_settle`, `_escalate`, `_do`, `_gate`, `_reserve`, `_ensure`, `_mark`, `_read`, `_call_step`, `_audit_call`, `_pivot_may_have_taken_effect`, `_compensate`, `_comp_done`, `_refund`, `_release`, and `RuntimeConfig` / `SAGAS` |
| `src/shape/runtime/store.py` | `Intent`, `SagaRecord`, `TERMINAL`, and the copy, lookup, ordering, and crash contracts of `Store._write`, `put_saga`, `has_saga`, `saga`, `open_sagas`, `put_intent`, `intent`, `intents_of`, `audit` |
| `src/shape/sim/host.py`, `src/shape/sim/faults.py` | `Host.call`, `Host.crash_at`, call outcomes, the crash cap, and burst faults (modeled as runs of the same errors) |
| `src/shape/sim/tools/{approvals,db,email,payments}.py` | `SimApprovals.request`, each fake's `execute`, and `SimPayments._charge` / `_refund` |
| `src/shape/simulation.py` | `Simulation._main` client (submit, ack, resubmit) and recovery loop, `place_order` fixtures, and `replay`; `saga_terminates` in `src/shape/invariants.py` for the submitted-order check |

## Check a change

1. Inspect `git diff 824fd3b38aa3f86ffe5d1942da200e2ab3748ea0 -- src/shape/runtime src/shape/sim src/shape/simulation.py`. Match the changed control flow and crash boundaries to the function and line references in `SagaProtocol.tla`. The model is hand-maintained. If the implementation changes an outcome or boundary, reflect it in the model and update the references before relying on TLC.
2. Run `verification/tla/run.sh` with the pinned TLC jar. Use `verification/tla/run.sh CASE` for the raw TLC result and trace from one config. Read each result with pstack's `/statechart` skill. Compare the reported outcomes with the README. The matrix treats its documented findings and deliberate mutations as expected failures.
3. Run `PYTHONPATH=src python verification/tla/replay.py` with Python 3.12 or later. To isolate the gate check, append `approval`. This replay checks the actual durable audit and tests a human rejection, a silent approver, and both immediate and delayed approval.
4. Translate any new counterexample to `ExplicitChoices` and check the store, audit, and world after replay. Match the individual writes and tool effects. If replay diverges, inspect the abstraction and replay choices before classifying the result. Keep a confirmed runtime violation visible with its counterexample.
5. Advance the sync commit only after confirming that the model describes the covered source at that commit, running the matrix, and confirming that the relevant negative control still fails. For changes that skipped this check, use pstack's `/maintain-formal-verification-skill`.

A stable key should produce at most one charge and one refund. Each email needs a previously recorded approval for its exact arguments. Every order the client submits should reach commit, compensation, or audited escalation: the client resubmits until it has the ack that follows the first durable write. After a crash every intent still started is in doubt, and once the email may have been sent the saga commits or escalates and never compensates. Keep the first-fix mutants (`doubt-mutant`, `retry-mutant`, `ack-mutant`, `pivot-mutant`) failing when the model changes. See the README for the findings and bounds.
