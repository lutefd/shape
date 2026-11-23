# Boundary mode: test your own agent

Boundary mode tests agent code you already have, in any Python framework, without rewriting it. You mark the tool functions that have side effects. shape injects faults and real process crashes at those boundaries, records every real effect outside the agent process, checks invariants, and shrinks any failure to a minimal replayable trace.

## User-facing API

```python
# tests/shape_checks.py
import shape

@shape.effect("payments.charge", scope=lambda order_id, **_: f"order:{order_id}")
def charge(order_id: str, amount: int) -> dict:
    return payments_client.charge(order_id, amount)          # your code, your test double

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
shape test tests/shape_checks.py::place_order --replay runs/place_order/seed-41.min.json
```

From pytest:

```python
def test_place_order_is_crash_safe():
    shape.check(place_order, seeds=200)    # raises AssertionError carrying the minimized trace
```

Approvals are recorded by the code that obtains them: `shape.approval(scope, args)` (for example, inside the node that receives the human decision). `approved_before(op)` requires an approval with the same scope and the same argument hash before every effect of `op`.

## How one seed runs

1. The parent process imports the scenario module once. It starts no threads.
2. For each seed, the parent forks a child per incarnation (spawn where fork is unavailable). Incarnation 0 runs the scenario with `run.restarts == 0`.
3. Inside the child, each call to an `@shape.effect` function draws its fault from the choice source at key `fault:<op>#<incarnation>.<n>` (n counts calls to that op in this incarnation, under a lock, so threaded frameworks still get stable keys per op). Value 0 runs the function normally.
4. A crash fault ends the child with `os._exit(137)`, either before the function runs or after it returns (the effect landed, the caller never sees it). Nothing in the child gets to clean up, flush, or checkpoint. That is the point.
5. The parent sees the child died and forks the next incarnation with `run.restarts` incremented, up to `max_restarts`. A scenario that has not finished after `max_restarts` is a `scenario_terminates` failure.
6. Every real effect and approval is appended and fsynced to the world log at `run.dir/world.jsonl` before the call returns to the agent, so the log survives the crash. Every nonzero draw is appended to `run.dir/realized.jsonl` the same way.
7. After the last incarnation, the parent evaluates the invariants over the world log. A failure is shrunk with ddmin over the realized map, replaying through `ExplicitChoices` in fresh forks, then printed and written as a replay file.

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

The exception a fault raises is what the user's stack would really see. Each `@shape.effect` takes `errors={"timeout": lambda: httpx.ReadTimeout("..."), ...}`. Defaults are `TimeoutError`, `ConnectionResetError`, and a `shape.RateLimited` / `shape.ServerError` pair. Retry behavior in the framework under test depends on the exception type, so this must be configurable per tool.

Swarm testing applies: each seed enables a random subset of fault kinds.

## What boundary mode does not control

Threads, timers, and the model inside the agent process are not virtualized. Fault keys are per op and per incarnation, so a run is reproducible when the agent's sequence of calls to each op is reproducible, which holds for scripted models and sequential graphs. When a replay diverges, the report says so instead of claiming a minimal trace. Model control (scripted, replayed, or perturbed model outputs) is a separate wrapper and is not in the first version.
