# Boundary mode: API reference

> New to `shape`? Start with the [User Guide](guide/concepts.md) ([Invariants](guide/invariants.md), [Reading a trace](guide/reading-a-trace.md), [Plain Python](guide/plain-python.md), [LangGraph](guide/langgraph.md), [OpenAI Agents SDK](guide/openai-agents.md)). This document is the complete contract for boundary mode.

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

`@shape.scenario(invariants=, max_restarts=3, actors=(), duplicate_trigger=False, expect_effects=True)`. `actors` and `duplicate_trigger` are described under Faults. `expect_effects=False` says the scenario may legitimately call no effect (a rejected request must do nothing); see Coverage.

### Effects, scopes and idempotency keys

`@shape.effect(op, scope=, key=, irreversible=, errors=, idempotent=, result=, hide=)` marks the function that changes the outside world. It works on `def` and `async def` functions (an async effect is recorded when the awaited call returns). Outside a shape run it is the plain function. `scope` and `key` are called with the call's named arguments (defaults applied), so `lambda order_id, **_: ...` works for any signature, methods included.

The world log counts real effects, not calls. Calls with the same `op`, `scope` and a non-null `key` are one effect, because the provider dedupes by key; calls without a key each count. This is what lets an idempotent retry pass `at_most_once` while a retry that drops the key fails it. shape trusts the key: a test double that ignores it is a bug in the double.

- `idempotent=True`: repeating a call with the same op, scope and arguments is one effect (a PUT-style write), whether or not it carries a key. The same op and scope with different arguments are two effects.
- `result=fn`: `fn(return_value)` is stored as the effect's `result` in the world log, for invariants to read. Without it `result` is `None`. If `fn` raises, the effect is still logged, with the error text in `result`.
- `hide=("secret", ...)`: arguments left out of the rendered call, the world log and the argument hash. These names are always hidden because frameworks inject them: `self`, `cls`, `config`, `runtime`, `callbacks`, `run_manager`, `tool_call_id`, `context`. `scope`, `key`, `result` and the fault `errors` still see every argument; approvals hash the visible ones, so `shape.approval(scope, args)` names the business arguments only.

`attempts` are the other half of the log: every call is logged before its fault is applied, so failed and killed calls are visible to `max_calls` and custom invariants even though they are not effects.

Invariants:

- `at_most_once(op)`: each scope has at most one real effect of `op`.
- `exactly_once(op)`: at most once per scope, and when the scenario finished, at least one effect of `op` overall (a lost effect fails it).
- `approved_before(op)`: every effect of `op` has an earlier approval with the same scope and argument hash.
- `approved_before(op, match=None)`: with `match(effect_args)`, the effect's named arguments (hidden ones left out) are mapped to what the human approved before hashing, for effects that sit at a lower layer than the tool the human saw (a raw API body vs the tool's arguments). The scope must still match.
- `requires(op, before=other_op, same_scope=True)`: every `op` effect has an earlier `other_op` effect that landed, for the same scope (any scope with `same_scope=False`). "Landed" means it is in the world log, so a charge that timed out after commit counts and a charge that timed out before commit does not. Catches "emailed 'you were charged' when the charge never landed".
- `within_scopes(op, allowed)`: every `op` effect acts on a scope in `allowed`, a collection of scopes or a function of the final `shape.Run` that returns one (it can read `run.dir`). Catches wrong-target actions under model perturbation.
- `max_calls(op, n, per_scope=True)`: at most `n` calls of `op` (per scope or in total), failed and killed calls included. Catches retry storms.
- `sum_le(op, field, limit, per_scope=True)`: the numeric argument `field` of real `op` effects sums to at most `limit`. Catches over-refunds. Non-numeric or missing values count as 0.
- `claims_true()`: every `shape.claim(scope, op)` the agent recorded when it told the user an action happened has an earlier effect of `op` for `scope` that landed. Catches "I've refunded you" when no refund exists.
- `@shape.invariant`: turns `fn(world) -> str | None` into an invariant; return a one-line plain-words message when the run is wrong. The function name is the invariant's name. An invariant that raises is reported as a violation ("the invariant itself raised ..."). See World below.
- `scenario_terminates()`: always on. The scenario must return within `max_restarts` restarts. When `max_restarts > 0`, the last incarnation runs with faults off (faults eventually stop), so this checks that the agent recovers from whatever state the faults left, not that it survives unbounded faults. With `max_restarts=0` every fault is live and the agent must handle it inline.

Each failure message is one line in plain words, for example `t.notify(order='1') happened with no earlier t.charge that landed for scope 'order:1'`. `exactly_once` has its own wording ("it must happen exactly once") and does not reuse `at_most_once`'s.

Claims are recorded where the agent makes the statement: `shape.claim(scope, op)`.

Approvals are recorded by the code that obtains them: `shape.approval(scope, args)` (for example, inside the node that receives the human decision). `args` is the dict of the effect's named arguments (without `self`); the hash is of its sorted JSON, so arguments must have stable reprs.

### Model choices

The model is an untrusted input, so let shape decide what it does at the points that matter. In your scripted model (or any code that stands in for one), call `shape.choose(label, options)`:

```python
def after_tool_error(messages):
    action = shape.choose("after_tool_error", ["retry_once", "give_up", "retry_twice"])
    ...
```

`options[0]` is the nominal choice, what a well-behaved model does. Each seed may pick another option instead, drawn at key `model:<label>#<incarnation>.<n>`. Choices are shrunk and replayed exactly like faults, so a failure that needs the model to repeat a call shows up in the minimized trace as `model chose 'refund_again' at 'after_refund' (nominal: 'reply_to_customer')`. Outside a run, and on the last fault-free incarnation, `choose` returns `options[0]`. `weights` defaults to the nominal option being 10 times as likely as each alternative. `shape.choose(label, options, weights=None, nominal=None)`: `nominal` is the text the trace uses for `options[0]` when its repr does not say what the model was supposed to do. The adapters pass the planned call, so a trace reads `model chose 'repeat' at 'call 2: issue_refund' (nominal: issue_refund(order_id='17'))`.

### World

A custom invariant reads the world log through `shape.World`. Entries are plain dicts in the order they were logged.

| Member | What it returns |
| --- | --- |
| `effects(op=None, scope=None)` | real effects (calls that are one effect counted once). Keys: `op`, `scope`, `key`, `call` (the rendered call), `args` (named arguments, hidden ones left out), `result` (what `result=` returned), `fault` (the fault injected on that call, if any), `inc` (incarnation), `twin` |
| `attempts(op=None, scope=None)` | every call, failed and killed ones included. Keys: `op`, `scope`, `call`, `fault`, `inc` |
| `approvals` | `shape.approval(...)` entries: `scope`, `args_hash` |
| `claims` | `shape.claim(...)` entries: `scope`, `op` |
| `completed` | the scenario returned within `max_restarts` |
| `run` | the final `shape.Run`, so you can read `run.dir` |

```python
@shape.invariant
def refunds_match_the_order(world):
    for e in world.effects("payments.refund"):
        if e["result"]["amount"] != e["args"]["amount_cents"]:
            return f"refunded {e['result']['amount']} but asked for {e['args']['amount_cents']}"
```

### Coverage and vacuous passes

After every sweep shape prints what it exercised, per op: effects that landed, attempts, and the faults injected by kind, then the model choices taken:

```
coverage over 200 seeds:
  op                landed  attempts
  payments.charge      202       252
                   faults injected: client_error 9, crash_after 6, timeout 9, ...
  model choices taken: after_refund -> 'refund_again' x5
```

A pass that tested nothing is not a pass. When no seed called any `@shape.effect` function (for example the model's output failed validation and the agent never reached its tools) and nothing failed, the sweep fails with `NOTHING TESTED` and exits 1. A scenario that must legitimately do nothing sets `expect_effects=False`. When an invariant watches an op that no seed ever called, the report adds a `note:` line (it does not fail: "this must never run" is a valid thing to assert).

### Flaky seeds

Boundary mode does not control threads, timers or an unscripted model, so a failing seed is re-run (`RERUNS = 2` more times, same seed). A seed that does not fail the same invariant on every re-run is `FLAKY`: it is counted separately (`flaky=N`), listed, never minimized, and still makes the run exit 1, because a check that sometimes fails is not a pass. Its first run is kept under the runs dir.

### Distinct causes

Failing seeds are grouped by root cause. The signature is the violated invariant plus the ordered `(op, fault)` and `(model label, choice)` deviations of the minimized run. The report starts with `N distinct causes across M failing seeds`, then prints one minimized trace per cause with how many seeds reached it. Seeds go fewest deviations first; a seed that still contains every deviation of a cause already found joins it without being shrunk again (counts are attributions by deviation kind, not separate replays); any other seed is minimized. At most 200 seeds are minimized per sweep, and the report says how many were not.

## How one seed runs

1. The parent process imports the scenario module once. It starts no threads.
2. For each seed, the parent forks a child per incarnation (spawn where fork is unavailable). Incarnation 0 runs the scenario with `run.restarts == 0`.
3. Inside the child, each call to an `@shape.effect` function draws its fault from the choice source at key `fault:<op>#<incarnation>.<n>` (n counts calls to that op in this incarnation, under a lock, so threaded frameworks still get stable keys per op). The value is an index into `[nominal, *faults]` in the table order below; 0 runs the function normally. A call is recorded as an effect when the function returns (or when an after-commit fault is injected), so a function that raises is not an effect.
4. A crash fault ends the child with `os._exit(137)`, either before the function runs or after it returns (the effect landed, the caller never sees it). Nothing in the child gets to clean up, flush, or checkpoint. That is the point.
5. The parent sees the child died and forks the next incarnation with `run.restarts` incremented, up to `max_restarts`. An incarnation ends one of four ways: the scenario returned (done), an injected crash (exit 137), an uncaught exception (the process died, so it restarts like any other death; the trace shows the root cause: the exception shape injected, or the innermost one in its `__cause__`/`__context__` chain, with the framework wrapper that surfaced it in parentheses), or no return within 30 seconds (killed). A scenario that has not finished after `max_restarts` is a `scenario_terminates` failure, reported with the last exception. Each incarnation's stdout and stderr go to `run.dir/inc-<n>.out`.
6. Every real effect and approval is appended and fsynced to the world log at `run.dir/world.jsonl` before the call returns to the agent, so the log survives the crash. Every call is also logged as an attempt before its fault is applied, and every nonzero draw is appended to `run.dir/realized.jsonl` the same way.
7. After the last incarnation, the parent evaluates the invariants over the world log. A failing seed is first confirmed: its realized map replays through `ExplicitChoices` in a fresh run, and the replay must fail the same invariant and reach every recorded key. Then ddmin shrinks the map, the minimal map is replayed once more and checked the same way, and the failure is printed in the code's own words (function names and arguments, faults, deaths) and written to `seed-<n>.min.json`. The summary counts `deviations` (injected faults, model choices, a duplicate delivery) kept out of those the seed took. Failing seeds are first re-run to rule out flakiness and then grouped into distinct causes (see above).
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
| `client_error` | the tool's 4xx exception (`shape.ClientError` by default) | no | documented |

The exception a fault raises is what the user's stack would really see. Each `@shape.effect` takes `errors={"timeout": lambda: httpx.ReadTimeout("..."), ...}`, keyed by fault name; a `timeout` factory also serves `timeout_after_commit`. Defaults are `TimeoutError`, `ConnectionResetError`, and a `shape.RateLimited` / `shape.ServerError` / `shape.ClientError` trio. Retry behavior in the framework under test depends on the exception type, so this must be configurable per tool.

`client_error` is a 4xx other than 429 returned before the commit (a declined card, a validation error, a conflict): the provider rejected the request and nothing changed. Its provenance is documented HTTP semantics (RFC 9110: a 4xx means the request was not fulfilled). A retry policy that retries it is a finding when the tool's own docs say it is not retryable; configure the tool's real exception (a 402, a 409) with `errors={"client_error": ...}`. A client error after commit is not modeled.

### Scenario-level faults and world actors

`@shape.scenario(duplicate_trigger=True)` adds one more fault to the scenario, drawn once per seed at key `world:duplicate_trigger` (swarm applies; about one seed in four when enabled). The scenario's first incarnation is then started twice at the same moment on the same `run.dir`, as an at-least-once queue or webhook delivers a message twice. They are two forked twins. Their draw keys are namespaced per twin (`fault:<op>#0.<n>` for copy A, `fault:<op>#0t1.<n>` for copy B), so each replays deterministically. The trace shows both copies (`incarnation 0 (copy A of the duplicate delivery)`, `(copy B ...)`). The incarnation counts as finished if either copy finished; restarts, when needed, are single. It is opt-in because a one-shot job is not meant to survive double delivery. The real interleaving of the two copies is not controlled, so a failure that depends on it can show as FLAKY.

`@shape.actor` marks a function `fn(run)` that the parent process calls between incarnations (after one dies, before the next starts), with the `Run` of the incarnation about to start. It changes the user's state while the agent is down: edit a file or row, revoke an approval. It uses `shape.choose` so a seed decides what the user did:

```python
@shape.actor
def user_edits_the_order(run):
    if shape.choose("user_edits_order", ["leave it", "raise the amount"]) == "raise the amount":
        (run.dir / "amount").write_text("999")

@shape.scenario(invariants=[shape.approved_before("transfers.send")], actors=[user_edits_the_order])
def stale_approval(run): ...
```

Actor choices are drawn at `model:<label>#<incarnation>a.<n>`, shrink and replay like any other, and show in the trace as `model chose 'raise the amount' at 'user_edits_order' (nominal: 'leave it') [world actor user_edits_the_order]`. Actors run in the parent, with no faults injected into any effect they call, and they do not run after the scenario finished. An actor that raises is reported as `world actor <name> raised ...` and the run continues.

Swarm testing applies: each seed enables a random subset of fault kinds. A call draws a fault with probability about 1 in 10 per enabled kind.

Each fault is drawn independently, so a seed can crash or fail every incarnation; that shows up as a `scenario_terminates` failure whose minimal trace is all those faults.

## What boundary mode does not control

Threads, timers, and the model inside the agent process are not virtualized, and the simulation's `seal` (clock, random, socket guards) is not applied: your code and your test doubles run for real. Fault keys are per op and per incarnation, so a run is reproducible when the agent's sequence of calls to each op is reproducible, which holds for scripted models and sequential graphs. When a replay diverges, the report says so instead of claiming a minimal trace. For deterministic model perturbations, use `shape.choose` directly or the scripted models in [docs/adapters.md](adapters.md).

## Reading the output

- Rendered calls leave out framework-injected arguments (`config`, `runtime`, `callbacks`, `run_manager`, `tool_call_id`, `context`) and any `hide=` names.
- `process raised: TimeoutError: shape: injected timeout (surfaced as RuntimeError: tool node failed)`: the root cause first, never a framework note such as `During task with name ...`.
- A scenario file that does not import prints one line, `shape: cannot import checks.py: ModuleNotFoundError: No module named 'x' (checks.py:3)`, and exits 2. `SHAPE_TRACEBACK=1` re-raises for the full traceback.
- When stdout is a closed pipe (`shape test ... | head -1`) shape exits quietly with status 141 and prints nothing to stderr.
