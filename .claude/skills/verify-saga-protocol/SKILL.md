---
name: verify-saga-protocol
description: Check the place-order saga's payment deduplication, durable email approval, termination, and compensation in TLA+. Use when changing Runtime or Store, including retry keys, crash recovery, pending actions, approval ordering, or compensation.
---

# Verify the saga protocol

Run from the repository root. The model and limits are in [verification/tla](../../../verification/tla/README.md).

## Coverage

Last source sync commit is `880c629b68f541e368031ff996c26baef0ce3109`.

| File | Functions and contracts covered |
| --- | --- |
| `src/shape/runtime/runtime.py` | `run`, `recover`, `_drive`, `_handle`, `_args`, `_done`, `_reject`, `_settle`, `_escalate`, `_do`, `_gate`, `_reserve`, `_ensure`, `_mark`, `_read`, `_call_step`, `_audit_call`, `_compensate`, `_comp_done`, `_refund`, `_release`, and `RuntimeConfig` / `SAGAS` |
| `src/shape/runtime/store.py` | `Intent`, `SagaRecord`, `TERMINAL`, and the copy, lookup, ordering, and crash contracts of `Store._write`, `put_saga`, `saga`, `open_sagas`, `put_intent`, `intent`, `intents_of`, `audit` |
| `src/shape/sim/host.py`, `src/shape/sim/faults.py` | `Host.call`, `Host.crash_at`, call outcomes and the crash cap |
| `src/shape/sim/tools/{approvals,db,email,payments}.py` | `SimApprovals.request`, each fake's `execute`, and `SimPayments._charge` / `_refund` |
| `src/shape/simulation.py` | `Simulation._main` initial submission and recovery loop, `place_order` fixtures, and `replay` |

## Check a change

1. Inspect `git diff 880c629b68f541e368031ff996c26baef0ce3109 -- src/shape/runtime src/shape/sim src/shape/simulation.py`. Match the changed control flow and crash boundaries to the function and line references in `SagaProtocol.tla`. The model is hand-maintained. If the implementation changes an outcome or boundary, reflect it in the model and update the references before relying on TLC.
2. Run `verification/tla/run.sh` with the pinned TLC jar. Use `verification/tla/run.sh CASE` for the raw TLC result and trace from one config. Read each result with pstack's `/statechart` skill. Compare the reported outcomes with the README. The matrix treats its documented findings and deliberate mutations as expected failures.
3. Run `PYTHONPATH=src python verification/tla/replay.py` with Python 3.12 or later. To isolate the gate check, append `approval`. This replay checks the actual durable audit and tests a human rejection, a silent approver, and both immediate and delayed approval.
4. Translate any new counterexample to `ExplicitChoices` and check the store, audit, and world after replay. Match the individual writes and tool effects. If replay diverges, inspect the abstraction and replay choices before classifying the result. Keep a confirmed runtime violation visible with its counterexample.
5. Advance the sync commit only after confirming that the model describes the covered source at that commit, running the matrix, and confirming that the relevant negative control still fails. For changes that skipped this check, use pstack's `/maintain-formal-verification-skill`.

A stable key should produce at most one charge and one refund. Each email needs a previously recorded approval for its exact arguments. Once a saga is persisted, it should reach commit, compensation, or audited escalation. An order submitted before its first durable write can disappear on crash. A compensated saga should have no email effect, and the current runtime can violate that rule after a send followed by a crash. See the README for the failing paths and bounds.
