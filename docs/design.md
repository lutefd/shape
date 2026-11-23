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
| `fault:<tool>.<op>#<n>` | host | index into `[nominal, *FAULTS]` (see `sim/faults.py`): which fault fires on the n-th call of that op |
| `burst:<tool>.<op>#<n>` | host | only drawn when a burst fault fires on that call: index into the burst lengths `[2, 3, 4, 6, 8]` (0 = shortest). The next calls to the same op get the base fault without drawing |
| `crash:call:<tool>.<op>#<n>` | host | 1 = the process dies before the call, 2 = after the effect lands, before the caller sees it |
| `crash:durable:<table>#<n>` | store | 1 = the process dies before the durable write lands, 2 = just after |
| `human:<request>` | simulated approver | 1 = reject, 2 = delay 30s, 3 = never answer. Drawn once per request, then fixed |
| `actor:<name>#<n>` | external actor | n-th tick (every 0.1s): 1 = stock +5, 2 = stock -1 on the inventory record |
| `sched#<n>` | event loop | n-th time two or more callbacks were ready: k runs the k-th ready callback first |
| `id:<ns>#<n>`, `jitter:<ns>#<n>` | seeded IDs and jitter | a value, not a deviation (excluded from shrinking). Milestone 1 uses only `jitter:latency#<n>`; IDs come from counters |

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
| `sim/host.py` | `Host`, the one pipeline every tool call and durable write goes through: crash draw, fault draw, latency, execute against the fake, after-effect crash or lost response |
| `sim/tools/` | verified-fake candidates: `SimDB` (versioned records, compare-and-set), `SimPayments` (charge, refund, idempotency keys with Stripe semantics), `SimEmail` (outbox, idempotent by key), `SimApprovals` (human approver). Each declares `ToolSpec` schemas and which ops are irreversible, and states the real API it mirrors |
| `runtime/` | the system under test for milestone 1, a toy saga runtime: durable intent log (`store.py`), idempotency keys, approval gate, audit log, compensation, crash recovery (`runtime.py`). Saga `toy` is `retry_key="fresh"` (the planted bug), saga `fixed` is `retry_key="stable"` |
| `policies.py` | `Policy.decide(step, observation) -> Action`. `AdversarialPolicy`, `ReplayPolicy`, `PerturbedReplay`, `RecordingPolicy` |
| `invariants.py` | oracles over `(world, durable state, trace)` |
| `shrink.py` | ddmin over `realized` |
| `report.py` | the few deviations that mattered, rendered for a human |
| `simulation.py` | `Simulation(seed, ...)`, which wires tools, faults, policy, runtime, and invariants and runs one seed |
| `cli.py` | `shape run --seeds N`, `shape replay <file>`, `shape shrink <file>` |

## Crashes

Crash is a `BaseException` subclass, so `except Exception` in the system under test cannot swallow it. The host raises it from inside a tool call or a durable write. That can happen before the call, or after the effect lands but before the caller sees the result. A write can be lost (crash before) or land (crash after). The simulation then discards the runtime instance, which loses its in-memory state, builds a fresh one on the same durable store, and calls `recover()`. At most 3 crashes happen per run. The policy (the model) is outside the process and survives a crash.

## Faults

Each fault kind carries its provenance. A finding states the provenance of every fault in its minimized trace. A double charge that needs only documented faults is a stronger finding than one that needs a theoretical fault.

## Faults in the table

`latency`, `timeout` (client gives up before commit), `timeout_after_commit` (commits, response lost), `duplicate_delivery`, `rate_limit`, `transient_5xx` (before commit), `compensation_failure` (applies to compensating calls only), plus `crash` and `external_modification` which are drawn at their own keys. Each seed enables a random half of the kinds (swarm testing).

Independent faults never produce the failure runs real providers do, so two kinds are correlated. `rate_limit_burst` (429s while a shared quota stays exhausted, past the advertised retry-after) and `outage_burst` (5xx while a dependency is down) hit the drawing call and then the next 1 to 7 calls to the same op, longer than the runtime's four attempts. Their provenance is `theoretical`: the single 429 or 5xx is documented, but we have neither docs for nor an observation of the persistence. A finding that needs a burst says so. Process crashes inside a tool call are drawn five times as often as at a durable write (`CALL_CRASH_WEIGHT`), because a call is in flight for real time and a write is not.

## The client

The simulation is also the client that submits the order. It records the order in `world.submitted` before handing it over, treats it as accepted only on the ack (`Runtime.run` acks after `submit`, the first durable write), and with no ack resubmits after the restart. `submit` is idempotent by order id. `saga_terminates` checks every order in `world.submitted`, not only persisted rows, so an order that never became a saga fails instead of passing vacuously.

## Saga end states

A saga ends `committed`, `compensated`, or `escalated`. `escalated` means the runtime could not finish or undo the saga (an irreversible step with unknown outcome, or a compensation that kept failing) and said so in the audit log. `saga_terminates` accepts it only with that audit entry, and the other invariants skip `rollback_restores_state` for it.

An intent is in doubt (`Intent.unknown`) when an attempt timed out or a crash cut it off. A new process has no call in flight, so `recover()` first marks every intent still `started` as in doubt, durably, and only then resumes. The email is the pivot: once it may have taken effect (its intent is not definitively `failed`) the saga can commit or escalate, and `_compensate` escalates instead of undoing anything.

## Invariants (milestone 1)

1. `at_most_once_effect`: each business scope (an order's charge, an event's email) has at most one real effect.
2. `gate_before_irreversible`: every irreversible effect has an earlier recorded gate approval for that exact action.
3. `rollback_restores_state`: a compensated saga leaves records equal to their before-state plus whatever outsiders changed meanwhile (the world tracks that), except for conflicts the runtime flagged; every charge is refunded; and it has no irreversible effect.
4. `no_invisible_effects`: every real effect appears in the audit log.
5. `compensation_idempotent`: after the run, with faults off, `compensate_again()` re-runs every finished compensation and the world is unchanged.
6. `saga_terminates`: once recovery runs to quiescence, every saga is committed, compensated, or escalated with an audit entry. A deadlock or a run past 10,000 virtual seconds fails it.

## Fakes

Fakes are hand-written and model only the semantics the invariants depend on: resource state machines, idempotency behavior, error classes and retry safety, and visibility. Each fake records the real API version it mirrors. A contract suite that runs the same tests against the fake and the real sandbox comes after milestone 1. Databases are the exception to faking. `SimDB` is a versioned record store, not SQL, and real Postgres under simulator-controlled interleaving is planned work.
