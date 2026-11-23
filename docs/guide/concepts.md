# Core concepts

`shape` tests whether the code around an AI agent keeps its safety properties when external calls fail, the process dies mid-step, or the model makes a bad decision. This page defines every term you will see in code and in CLI output. For the full API contract, see [docs/boundary.md](../boundary.md).

## Effects, ops, scopes, and keys

An **effect** is a call that changes something outside the agent process: charging a card, sending an email, issuing a refund, or writing to an external API. You mark the function that performs it with `@shape.effect`:

```python
@shape.effect(
    "payments.charge",
    scope=lambda order_id, **_: f"order:{order_id}",
    key=lambda idempotency_key=None, **_: idempotency_key,
)
def charge(self, order_id: str, amount: int, idempotency_key: str | None = None) -> int:
    ...
```

Outside a `shape` run, the decorator returns the plain function with no overhead. It supports both `def` and `async def` functions. For an `async def` function, faults and logging happen when the coroutine is awaited, not when it is created.

- **op**: A dot-separated name for the action, such as `"payments.charge"` or `"email.send"`. Invariants reference this name.
- **scope**: A function returning a string that identifies the business entity affected, such as `"order:17"`. Invariants like `at_most_once` and `requires` group effects by scope. If omitted, the scope is `""` (the whole run).
- **key**: A function returning the idempotency key sent to the external provider, or `None`. Calls with the same `op`, `scope`, and non-null `key` count as **one real effect** in the world log, because the provider deduplicates them. Calls with `key=None` (or distinct keys) each count as a separate effect.
- **key_from_result**: A function `fn(return_value) -> str | None` that derives a deduplication key from the return value (for tools that dedupe via state and return the same resource ID such as `"CMP-5170"` on a repeat call). The derived key is recorded in `world.jsonl`, so repeat calls yielding the same ID count as one effect.
- **landed**: A predicate `fn(return_value) -> bool` for tools that report refusal or failure via return values instead of raising. When falsy, the call is logged as an attempt that `returned without effect`, not a landed effect.
- **idempotent**: Pass `idempotent=True` for PUT-style writes where repeating the same `op`, `scope`, and visible arguments produces one effect even without an idempotency key. Calling the same `op` and `scope` with different arguments still counts as two effects.

`scope` and `key` receive the call's bound arguments as keyword arguments, with defaults applied and `**kwargs` unpacked (plus adapter-supplied framework context). Always accept `**_` so the lambda works on any signature, including methods with `self`. `result`, `landed`, and `key_from_result` take the return value and may also accept any named argument or framework context via keyword parameters or `**kwargs`. If any hook raises, `shape` raises `shape.HarnessError` (`BaseException`) and fails the run immediately pointing at your lambda — framework error handlers never swallow it.

### Where to put `@shape.effect`

Put `@shape.effect` on the function that actually touches the outside world, such as your HTTP or SDK client method. Do not put it on an outer agent tool if that tool:

- checks agent state first and returns early without calling the provider (marking the outer tool would log a fake effect and inject faults on the no-op path), or
- runs its own retry loop around the client (marking the outer tool hides the inner attempts from `shape`, so `shape` cannot fault individual retries).

The framework adapters (`shape.langgraph.effects` and `shape.agents.effects`) wrap tool objects directly for convenience when each tool makes one external call. When you can only wrap an agent-facing tool that dedupes via state or returns refusals as strings/dicts, pass `key_from_result=` and `landed=` so the world log still matches reality.

### Tools that report failure via return values

By default, `shape` records a call as a landed effect whenever the wrapped function returns without raising an exception. If your tool reports refusals or errors by returning a value (such as `"not the owner"` or `{"ok": False, "error": "declined"}`), pass `landed=lambda res: ...`:

```python
@shape.effect(
    "orders.cancel",
    scope=lambda order_id, **_: f"order:{order_id}",
    landed=lambda res: bool(res.get("ok")),
)
def cancel_order(order_id: str) -> dict: ...
```

When `landed` returns `False`, the call is logged as an attempt that did not land (`returned without effect` in traces), not an effect, and after-commit faults are not injected. In the framework adapters, LangChain `ToolMessage(status="error")` responses and OpenAI Agents SDK `"An error occurred while running the tool."` error strings are detected as not landed automatically without writing `landed=`.

## World log vs. the agent's beliefs

Every `shape` seed writes to an isolated directory `run.dir`. Your agent stores its own checkpoints, SQLite files, or session state in `run.dir`. Separate from that, `shape` maintains two append-only JSONL files in `run.dir`:

- `world.jsonl` (the **world log**): records every call attempt before faults run (`attempt`), every real effect after the call returns (`effect`), every `shape.approval(...)`, and every `shape.claim(...)`. Each line is appended and `fsync`ed before the function returns, so it survives `os._exit(137)`.
- `realized.jsonl`: records every non-nominal decision taken during the run (injected faults, non-nominal model choices, and duplicate triggers).

Invariants evaluate the world log, not the agent's database or message history. The agent may believe a charge failed after a connection reset or crash; the world log records that the charge landed.

`world.jsonl` tracks both **attempts** and **effects**:

- An **attempt** is logged before any fault fires. Failed and killed calls count as attempts. `max_calls` reads attempts.
- An **effect** is logged when the call completes (including before an after-commit fault or `crash_after`). Before-commit faults and `crash_before` never log an effect. `at_most_once`, `exactly_once`, `requires`, `sum_le`, `within_scopes`, `approved_before`, and `claims_true` read effects.

## Incarnations and restarts

Each seed runs your `@shape.scenario` function in a separate child process (via `os.fork` on Unix, or `spawn` where fork is unavailable or when `SHAPE_START_METHOD=spawn`). Each process execution is an **incarnation**.

1. Incarnation `0` runs with `run.restarts == 0`.
2. If the incarnation exits cleanly, the scenario is complete.
3. If the incarnation dies from a crash fault (`os._exit(137)`), an uncaught exception, or the 30-second incarnation timeout, `shape` starts a new child process on the same `run.dir` with `run.restarts` incremented.
4. This repeats up to `max_restarts` restarts (default `3`, so at most 4 incarnations).

**Fairness on the last incarnation:** When `max_restarts > 0`, the final incarnation (`inc == max_restarts`) runs with faults and model perturbations turned off. That tests whether your agent can recover from whatever broken state earlier faults left behind, rather than testing whether it can survive an endless stream of failures. When `max_restarts == 0`, every fault is live and the scenario must handle failures inline without restarting.

Each incarnation's stdout and stderr are captured to `run.dir/inc-<n>.out`.

## Fault kinds and provenance

At each `@shape.effect` call, `shape` draws from `[nominal, *FAULTS]` at key `fault:<op>#<incarnation>.<n>`. Value `0` is nominal (the call runs untouched). Nonzero values inject one of eight per-call faults:

| Fault | What the caller sees | Effect landed? | Default exception | Provenance |
| --- | --- | --- | --- | --- |
| `timeout` | timeout exception | no | `TimeoutError` | documented |
| `timeout_after_commit` | timeout exception | yes | `TimeoutError` | documented |
| `connection_reset_after_commit` | connection reset | yes | `ConnectionResetError` | documented |
| `rate_limit` | 429 exception | no | `shape.RateLimited` | documented |
| `server_error` | 5xx exception | no | `shape.ServerError` | documented |
| `crash_before` | process killed (`os._exit(137)`) | no | none | documented |
| `crash_after` | process killed (`os._exit(137)`) | yes | none | documented |
| `client_error` | 4xx exception (other than 429) | no | `shape.ClientError` | documented |

All eight faults have **provenance `documented`**: they represent failure modes documented by HTTP specifications (RFC 9110), payment providers, and OS process models. A crash fault calls `os._exit(137)` directly, so `finally` blocks, context managers, and `atexit` handlers do not run.

Retry logic in your code or framework inspects exception types. Pass `errors=` to `@shape.effect` to raise your real client's exceptions:

```python
@shape.effect(
    "payments.charge",
    scope=lambda order_id, **_: f"order:{order_id}",
    errors={
        "timeout": lambda: httpx.ReadTimeout("read timed out"),
        "client_error": lambda: MyDeclinedError(402),
    },
)
```

A `timeout` factory also serves `timeout_after_commit` unless you override `timeout_after_commit` separately.

### Scenario-level faults and world actors

Two additional fault mechanisms operate at the scenario level:

- **Duplicate delivery (`duplicate_trigger=True`)**: On roughly 1 in 4 seeds when enabled on `@shape.scenario`, incarnation `0` starts as two forked copies at the same moment on the same `run.dir`. This models an at-least-once webhook or job queue delivering the same trigger twice. Copy A draws under `#0.<n>` and copy B draws under `#0t1.<n>`. The real OS interleaving between the two processes is not controlled, so race-dependent failures can surface as `FLAKY`. Deterministic concurrency control (`shape.point` and `run_async`) is planned.
- **World actors (`@shape.actor`)**: Functions passed in `@shape.scenario(actors=[...])` run in the parent process between incarnations (after one incarnation dies and before the next starts), with faults off. Use `shape.choose` inside an actor to model external state changes while the agent is down, such as a user editing an order amount or revoking an approval.

## Model choices (`shape.choose`)

The model is an untrusted input. Instead of hardcoding one happy-path script, let `shape` vary model decisions with `shape.choose(label, options, weights=None, nominal=None)`:

```python
action = shape.choose("after_refund", ["reply_to_customer", "refund_again"])
```

- `options[0]` is always the **nominal** choice (what a well-behaved model does).
- On a seeded run, `shape` draws at key `model:<label>#<incarnation>.<n>`. By default, `options[0]` has weight `10.0` and each alternative has weight `1.0`.
- Outside a `shape` run, and on the final fault-free incarnation, `shape.choose` always returns `options[0]`.
- Pass `nominal=` to customize how the nominal option is described in traces when `repr(options[0])` is not descriptive.
- Model choices are recorded in `realized.jsonl`, shrunk by delta debugging, and replayed identically to faults.

The LangGraph and OpenAI Agents adapters build on `shape.choose` inside `ScriptedChatModel` and `ScriptedModel` to test repeated calls, dropped calls, swapped call order, argument substitution, retry counts, and post-restart re-issues.

## Invariants

An **invariant** is a property checked against the world log after a seed finishes all incarnations. `scenario_terminates()` is always active. You pass additional invariants in `@shape.scenario(invariants=[...])`, choosing from the built-ins (`at_most_once`, `exactly_once`, `approved_before`, `requires`, `within_scopes`, `max_calls`, `sum_le`, `claims_true`) or writing your own with `@shape.invariant`. See [Invariants](invariants.md).

## Swarm testing

Each seed does not enable every fault kind at once. Instead, `SeededChoices` uses **swarm testing**: for each fault kind, the seed deterministically flips a 50/50 coin (`hash(seed, "swarm:<kind>")`) to decide whether that fault kind is enabled for the run. Each enabled fault kind gets weight `1.0` against nominal weight `10.0`.

Swarm testing increases bug-finding diversity across seeds: some seeds test crashes without timeouts, some test network errors without crashes, and some combine both. The swarm mask is a property of the seed generator and never appears in `realized.jsonl`.

## Shrinking and replay

When a seed violates an invariant, `shape` extracts its `realized` map (the keys whose drawn value was nonzero) and runs delta debugging (`ddmin`) over those keys using `ExplicitChoices`. Removing a key returns that draw to `0` (nominal).

1. `shape` first replays the full `realized` map in a fresh directory and verifies that it violates the same invariant and reaches every recorded key.
2. `ddmin` removes keys until no single remaining key can be removed without the failure disappearing (1-minimal).
3. `shape` replays the minimal map one final time, verifies it, writes `runs/<scenario>/seed-<n>.min.json`, and prints the minimized trace.

If either verification replay does not fail the same invariant or does not reach every recorded key, `shape` prints `NOT MINIMIZED` with the original run and does not claim a minimal trace.

You can replay any saved `.min.json` file directly:

```sh
shape test checks.py::my_scenario --replay runs/my_scenario/seed-52.min.json
```

Exit codes for `--replay`: `0` if the bug is fixed (and every recorded deviation was still reached), `1` if the invariant still fails, and `2` if the code diverged and no longer reached the recorded deviation keys.

## Coverage and `NOTHING TESTED`

After every sweep, `shape` prints a coverage summary over all seeds:

```text
coverage over 200 seeds:
  op                landed  attempts
  payments.charge      202       252
                   faults injected: client_error 2, connection_reset_after_commit 2, crash_after 11, crash_before 7, rate_limit 4, server_error 5, timeout 9, timeout_after_commit 12
  model choices taken: none
```

- **`NOTHING TESTED`**: If no seed called any `@shape.effect` function, or if attempts ran but zero effects ever landed, and nothing failed, the sweep fails with `NOTHING TESTED` and exits `1`. The message explains why when known: zero attempts (the agent never reached its tools), attempts that returned without effect (check `landed=` or tool refusals), attempts that all raised before completing, or harness hook errors. If a scenario legitimately expects zero effects, such as a rejected approval flow, set `@shape.scenario(..., expect_effects=False)`.
- **`note:` lines**: If an invariant watches an `op` that no seed ever called, `shape` prints `note: an invariant watches <op>, which no seed ever called`. This is informational and does not fail the sweep.

## `FLAKY` seeds

Boundary mode does not control OS thread scheduling, wall-clock timers, or live LLM calls inside the child process. To prevent false reports, `shape` re-runs every failing seed 2 additional times (`RERUNS = 2`) with the exact same seed.

If a seed does not violate the same invariant on all re-runs, `shape` classifies it as `FLAKY`:

- It is counted separately (`flaky=N`) and listed in the report.
- It is never minimized.
- Its first failing run directory is kept under `runs/<scenario>/seed-<n>/` for inspection.
- The sweep still exits `1`, because a test that sometimes fails is not a pass.

## Distinct causes

Many seeds in a sweep often trigger the same bug. `shape` groups failing seeds by **cause**: the violated invariant, the op that violated it, and the coarse class of each deviation in the minimized run:

- **after-commit fault** (`the caller did not see the commit`): `timeout_after_commit`, `connection_reset_after_commit`, and `crash_after` group into one cause, with the exact fault kinds seen across those seeds listed inside it.
- **before-commit fault** (`the call failed cleanly`): `timeout`, `rate_limit`, `server_error`, and `client_error` group into one cause, with exact kinds listed.
- **crash** (`died before the call`): `crash_before`.
- **model choice** and **duplicate delivery**: grouped by choice / trigger.

Each cause is labeled by what it took to trigger (`needs: after-commit fault (...)`, `needs: model choice`, etc.). Causes that fail on the nominal path with 0 faults and 0 model choices are printed **last** under `nominal path (0 faults, 0 model choices): a plain unit test catches this`, so findings that only fault injection or model perturbation can produce stand out first.

Seeds are processed in order of fewest deviations first. Once a minimized cause is found, any remaining failing seed with the same invariant and op whose raw coarse deviations are a superset of that cause's deviations joins it without running `ddmin` again (recording its exact fault kinds on that cause). At most 200 failing seeds are minimized per sweep. The report prints `N distinct causes across M failing seeds` and one minimized trace per cause.
