# shape: design

shape has two parts that share one core.

1. **Deterministic simulation testing (DST) for agent systems.** The model is an untrusted input source. The question is whether the safety properties of the system around the model hold under any sequence of agent decisions and world failures.
2. **A Jepsen for agent frameworks.** The same harness runs in boundary mode against third-party frameworks such as LangGraph. It tests what each framework's docs claim about durability, resume, retries, and approvals.

This document fixes the data shapes. Code in `src/shape/` follows it, and a change to a shape here changes the code in the same PR.

## The one shape everything hangs on: addressable choices

`src/shape/choices.py`. Every nondeterministic decision is a `draw(key, weights)` at a stable string key. Value 0 always means nominal: no fault, the correct action, FIFO scheduling.

| Key pattern | Who draws | Nonzero means |
| --- | --- | --- |
| `agent#<step>` | policy | a misbehavior kind instead of the nominal action |
| `agent#<step>:arg` | policy | which wrong argument, which earlier call to duplicate |
| `fault:<tool>.<op>#<n>` | fault injector | which fault kind fires on the n-th call of that op |
| `crash:<point>#<n>` | simulation | the agent process dies at that point |
| `human:<request>` | simulated approver | reject, delay, or never answer |
| `actor:<name>#<n>` | external actor | edits a record now |
| `sched#<n>` | event loop | runs a ready task other than the first |
| `id:<ns>#<n>`, `jitter:<ns>#<n>` | seeded IDs and jitter | a value, not a deviation (excluded from shrinking) |

A run is identified by `realized`, the map of keys whose drawn value was nonzero. It is the replay file and the shrinker's input. `SeededChoices(seed)` derives each value from `hash(seed, key)`, so it does not depend on draw order. `ExplicitChoices(realized)` replays it, and every unlisted key is 0. Swarm testing lives in the seeded generator only. Each seed enables a random subset of fault kinds, and that subset never appears in `realized`.

Shrinking is delta debugging over the keys of `realized`, ignoring `id:` and `jitter:` keys. Removing a key returns that decision to nominal. The result is the minimal set of deviations from the nominal run that still breaks the invariant.

## Modules

| Module | Owns |
| --- | --- |
| `choices.py` | `ChoiceSource`, `SeededChoices`, `ExplicitChoices` |
| `trace.py` | `Event(seq, t, kind, text, data)` and `Trace`, an append-only log of what happened, rendered for humans |
| `loop.py` | deterministic asyncio event loop with a virtual clock. Ready-queue order comes from `sched#n` draws. Sleeps and timeouts advance virtual time instantly. A run with nothing runnable and no timers raises `Deadlock` |
| `seal.py` | `sealed()`, which makes real clock reads, `time.sleep`, the `random` module, `uuid1/uuid4`, `os.urandom`, and socket connects raise `Escape` for the duration of a run |
| `sim/world.py` | `World`, the god's-eye record of real effects (`Effect(tool, op, scope, key, reversible, args, t)`) and resource state, separate from anything the agent or its audit log believes |
| `sim/faults.py` | the fault table: name, provenance (`documented`, `observed`, `theoretical`), which ops it applies to, default weight |
| `sim/tools/` | verified-fake candidates: `SimDB` (versioned records, compare-and-set), `SimPayments` (charge, refund, idempotency keys with Stripe semantics), `SimEmail` (outbox), `SimApprovals` (human approver). Each declares `ToolSpec` schemas and which ops are irreversible |
| `runtime/` | the system under test for milestone 1, a toy saga runtime: durable intent log, idempotency keys, approval gate, audit log, compensation, crash recovery |
| `policies.py` | `Policy.decide(step, observation) -> Action`. `AdversarialPolicy`, `ReplayPolicy`, `PerturbedReplay`, `RecordingPolicy` |
| `invariants.py` | oracles over `(world, durable state, trace)` |
| `shrink.py` | ddmin over `realized` |
| `simulation.py` | `Simulation(seed, ...)`, which wires tools, faults, policy, runtime, and invariants and runs one seed |
| `cli.py` | `shape run --seeds N`, `shape replay <file>`, `shape shrink <file>` |

## Crashes

Crash is a `BaseException` subclass, so `except Exception` in the system under test cannot swallow it. The simulation raises it from inside a tool call or a durable write. That can happen before the call, or after the effect lands but before the caller sees the result. The simulation then discards the runtime instance, which loses its in-memory state, builds a fresh one on the same durable store, and calls `recover()`.

## Faults

Each fault kind carries its provenance. A finding states the provenance of every fault in its minimized trace. A double charge that needs only documented faults is a stronger finding than one that needs a theoretical fault.

## Invariants (milestone 1)

1. `at_most_once_effect`: each business scope (an order's charge, an event's email) has at most one real effect.
2. `gate_before_irreversible`: every irreversible effect has an earlier recorded gate approval for that exact action.
3. `rollback_restores_state`: a compensated saga leaves reversible resources equal to their before-state, except for conflicts the runtime flagged, and has no irreversible effect.
4. `no_invisible_effects`: every real effect appears in the audit log.
5. `compensation_idempotent`: running every compensation again changes nothing in the world.
6. `saga_terminates`: once recovery runs to quiescence, every saga is committed or compensated.

## Fakes

Fakes are hand-written and model only the semantics the invariants depend on: resource state machines, idempotency behavior, error classes and retry safety, and visibility. Each fake records the real API version it mirrors. A contract suite that runs the same tests against the fake and the real sandbox comes after milestone 1. Databases are the exception to faking. `SimDB` is a versioned record store, not SQL, and real Postgres under simulator-controlled interleaving is planned work.
