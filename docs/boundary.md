# Boundary mode: test your own agent

Boundary mode tests agent code you already have, in any Python framework, without rewriting it. You mark the tool functions that have side effects. shape injects faults and real process crashes at those boundaries, records every real effect outside the agent process, checks invariants, and shrinks any failure to a minimal replayable trace.

## User-facing API

```python
# tests/shape_checks.py
import shape

@shape.effect("payments.charge",
              scope=lambda order_id, **_: f"order:{order_id}",
              key=lambda idempotency_key=None, **_: idempotency_key)
def charge(order_id: str, amount: int, idempotency_key: str | None = None) -> dict:
    return payments_client.charge(order_id, amount, idempotency_key)   # your code, your test double

@shape.effect("email.send", scope=lambda to, order_id, **_: f"order:{order_id}", irreversible=True)
def send_email(to: str, order_id: str, body: str) -> None: ...

@shape.scenario(
    invariants=[
        shape.at_most_once("payments.charge"),
        shape.exactly_once("payments.charge"),               # liveness: it did happen
        shape.approved_before("email.send"),
    ],
    max_restarts=3,
)
def place_order(run: shape.Run) -> None:
    graph = build_graph(checkpointer=SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")))
    config = {"configurable": {"thread_id": "order-17"}}
    if run.restarts == 0:
        graph.invoke({"order_id": "17"}, config)
    else:
        graph.invoke(None, config)                            # resume from your own persistence
```

```sh
shape test tests/shape_checks.py --seeds 500           # every scenario in the file
shape test tests/shape_checks.py::place_order --seeds 500 --start 1000 --jobs 4
shape test tests/shape_checks.py::place_order --replay runs/place_order/seed-41.min.json
```

`shape test` exits nonzero when any scenario fails (1) or a replay diverges (2). Runs go to `--runs-dir` (default `runs/`). `--jobs J` runs seeds in J forked workers, where fork exists.

From pytest:

```python
def test_place_order_is_crash_safe():
    shape.check(place_order, seeds=200)    # raises AssertionError carrying the minimized trace
```

`check(scenario, seeds=100, start=0, runs_dir="runs")`.

### Effects, scopes and idempotency keys

`@shape.effect(op, scope=, key=, irreversible=, errors=)` marks the function that changes the outside world. Outside a shape run it is the plain function. `scope` and `key` are called with the call's named arguments (defaults applied), so `lambda order_id, **_: ...` works for any signature, methods included.

The world log counts real effects, not calls. Calls with the same `op`, `scope` and a non-null `key` are one effect, because the provider dedupes by key; calls without a key each count. This is what lets an idempotent retry pass `at_most_once` while a retry that drops the key fails it. shape trusts the key: a test double that ignores it is a bug in the double.

Invariants:

- `at_most_once(op)`: each scope has at most one real effect of `op`.
- `exactly_once(op)`: at most once per scope, and when the scenario finished, at least one effect of `op` overall (a lost effect fails it).
- `approved_before(op)`: every effect of `op` has an earlier approval with the same scope and argument hash.
- `scenario_terminates()`: always on. The scenario must return within `max_restarts` restarts. When `max_restarts > 0`, the last incarnation runs with faults off (faults eventually stop), so this checks that the agent recovers from whatever state the faults left, not that it survives unbounded faults. With `max_restarts=0` every fault is live and the agent must handle it inline.

Approvals are recorded by the code that obtains them: `shape.approval(scope, args)` (for example, inside the node that receives the human decision). `args` is the dict of the effect's named arguments (without `self`); the hash is of its sorted JSON, so arguments must have stable reprs.

## How one seed runs

1. The parent process imports the scenario module once. It starts no threads.
2. For each seed, the parent forks a child per incarnation (spawn where fork is unavailable). Incarnation 0 runs the scenario with `run.restarts == 0`.
3. Inside the child, each call to an `@shape.effect` function draws its fault from the choice source at key `fault:<op>#<incarnation>.<n>` (n counts calls to that op in this incarnation, under a lock, so threaded frameworks still get stable keys per op). The value is an index into `[nominal, *faults]` in the table order below; 0 runs the function normally. A call is recorded as an effect when the function returns (or when an after-commit fault is injected), so a function that raises is not an effect.
4. A crash fault ends the child with `os._exit(137)`, either before the function runs or after it returns (the effect landed, the caller never sees it). Nothing in the child gets to clean up, flush, or checkpoint. That is the point.
5. The parent sees the child died and forks the next incarnation with `run.restarts` incremented, up to `max_restarts`. An incarnation ends one of four ways: the scenario returned (done), an injected crash (exit 137), an uncaught exception (the process died, so it restarts like any other death), or no return within 30 seconds (killed). A scenario that has not finished after `max_restarts` is a `scenario_terminates` failure, reported with the last exception. Each incarnation's stdout and stderr go to `run.dir/inc-<n>.out`.
6. Every real effect and approval is appended and fsynced to the world log at `run.dir/world.jsonl` before the call returns to the agent, so the log survives the crash. Every nonzero draw is appended to `run.dir/realized.jsonl` the same way.
7. After the last incarnation, the parent evaluates the invariants over the world log. A failing seed is first confirmed: its realized map replays through `ExplicitChoices` in a fresh run, and the replay must fail the same invariant and reach every recorded key. Then ddmin shrinks the map, the minimal map is replayed once more and checked the same way, and the failure is printed in the code's own words (function names and arguments, faults, deaths) and written to `seed-<n>.min.json`. Of the failing seeds, the one with the fewest faults is shrunk.
8. If either replay does not fail again or does not reach every recorded key, the run depends on something shape does not control. The report says `NOT MINIMIZED` with the original run and the reason, and claims no minimal trace.

## Faults at the boundary

| Fault | What the caller sees | Effect happened? | Provenance |
| --- | --- | --- | --- |
| `timeout` | the tool's timeout exception | no | documented |
| `timeout_after_commit` | the tool's timeout exception | yes | documented |
| `connection_reset_after_commit` | `ConnectionResetError` (or the tool's) | yes | documented |
| `rate_limit` | the tool's rate-limit exception | no | documented |
| `server_error` | the tool's 5xx exception | no | documented |
| `crash_before` | process dies | no | documented |
| `crash_after` | process dies | yes | documented |

The exception a fault raises is what the user's stack would really see. Each `@shape.effect` takes `errors={"timeout": lambda: httpx.ReadTimeout("..."), ...}`, keyed by fault name; a `timeout` factory also serves `timeout_after_commit`. Defaults are `TimeoutError`, `ConnectionResetError`, and a `shape.RateLimited` / `shape.ServerError` pair. Retry behavior in the framework under test depends on the exception type, so this must be configurable per tool.

Swarm testing applies: each seed enables a random subset of fault kinds. A call draws a fault with probability about 1 in 10 per enabled kind.

Each fault is drawn independently, so a seed can crash or fail every incarnation; that shows up as a `scenario_terminates` failure whose minimal trace is all those faults.

## What boundary mode does not control

Threads, timers, and the model inside the agent process are not virtualized, and the simulation's `seal` (clock, random, socket guards) is not applied: your code and your test doubles run for real. Fault keys are per op and per incarnation, so a run is reproducible when the agent's sequence of calls to each op is reproducible, which holds for scripted models and sequential graphs. When a replay diverges, the report says so instead of claiming a minimal trace. Model control (scripted, replayed, or perturbed model outputs) is a separate wrapper and is not in the first version.
