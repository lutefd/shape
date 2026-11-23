# Invariants

Invariants state what must hold in the outside world, regardless of crashes, network errors, or bad model decisions. Every invariant checks the world log (`shape.World`), which `shape` writes outside the agent process.

Every snippet below is runnable in [`docs/guide/examples/invariants.py`](examples/invariants.py):

```sh
shape test docs/guide/examples/invariants.py --seeds 50
```

For exact signatures and world-log schema, see [docs/boundary.md](../boundary.md).

## Built-in invariants

### `at_most_once(op)`

**Bug it catches:** Double charges, duplicate refunds, or repeated emails after a retry, crash, or repeated model call.

Each `scope` may have at most one real effect of `op`. Calls with the same `op`, `scope`, and non-null `key` deduplicate to one effect. Calls with `idempotent=True` and identical visible arguments also deduplicate to one effect.

```python
@shape.scenario(invariants=[shape.at_most_once("payments.charge")], max_restarts=1)
def retry_without_key(run: shape.Run) -> None:
    try:
        charge("1", 500, key="order-1")
    except (TimeoutError, ConnectionResetError):
        charge("1", 500)  # the first call may have landed; this one has no key
```

Real output:

```text
FAIL docs/guide/examples/invariants.py::retry_without_key  seed=7  invariant=at_most_once(payments.charge)
  payments.charge happened 2 times for scope 'order:1' (at most 1 allowed)
  minimized to 2 steps (1 of 1 deviations kept, 3 replays):
    incarnation 0
      1. charge(order='1', amount=500, key='order-1')
           effect landed (idempotency key 'order-1'), then timeout after commit: the caller saw TimeoutError
      2. charge(order='1', amount=500, key=None)
           effect landed
      finished
```

### `exactly_once(op, failure_claim_ok=False)`

**Bug it catches:** Both duplicate effects and lost effects (for example, marking a task done before the effect lands, then crashing or catching an error and never retrying).

Checks two things: at most one real effect per scope, and when the scenario finishes cleanly (`world.completed`), at least one real effect of `op` overall.

With `failure_claim_ok=True`, a lost effect is acceptable when the agent explicitly recorded a failure claim (`shape.claim(scope, op, ok=False)`) for every attempted scope and never claimed success (`ok=True`). Claiming success or finishing silently with no failure claim is still a violation.

```python
@shape.scenario(invariants=[shape.exactly_once("payments.charge")], max_restarts=3)
def mark_done_first(run: shape.Run) -> None:
    db = sqlite3.connect(run.dir / "progress.db")
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS done (step TEXT PRIMARY KEY)")
        fresh = db.execute("INSERT OR IGNORE INTO done VALUES ('charge')").rowcount
    if fresh:
        charge("1", 500, key="order-1")
```

Real output:

```text
FAIL docs/guide/examples/invariants.py::mark_done_first  seed=6  invariant=exactly_once(payments.charge)
  payments.charge never happened, but the scenario finished (a lost effect)
  minimized to 1 steps (1 of 1 deviations kept, 3 replays):
    incarnation 0
      1. charge(order='1', amount=500, key='order-1')
           timeout before the effect: the caller saw TimeoutError
      process raised: TimeoutError: shape: injected timeout
    incarnation 1 (restarted)
      finished
```

### `approved_before(op, match=None)`

**Bug it catches:** Irreversible actions running without human approval, after a rejection, or with arguments changed after approval (such as a larger amount or different recipient).

Record an approval where the human or policy grants it with `shape.approval(scope, args)`. `args` is a dict of the effect's visible business arguments (excluding hidden arguments like `self`, `config`, `runtime`, `callbacks`, `run_manager`, `tool_call_id`, `context`, and any names listed in `hide=`). `shape` hashes the sorted JSON of `args` and checks that every `op` effect has an earlier approval with the same `scope` and hash.

```python
@shape.scenario(invariants=[shape.approved_before("transfers.send")], max_restarts=1)
def wire_other_amount(run: shape.Run) -> None:
    shape.approval("order:1", {"order": "1", "amount": 100})
    wire("1", shape.choose("amount", [100, 1000]))
```

Real output:

```text
FAIL docs/guide/examples/invariants.py::wire_other_amount  seed=0  invariant=approved_before(transfers.send)
  wire(order='1', amount=1000) ran with no earlier approval for scope 'order:1'
  minimized to 3 steps (1 of 1 deviations kept, 3 replays):
    incarnation 0
      1. approval recorded for 'order:1'
      2. model chose 1000 at 'amount' (nominal: 100)
      3. wire(order='1', amount=1000)
           effect landed
      finished
```

If `@shape.effect` sits at a lower layer than the tool the human approved (for example, a raw HTTP body instead of structured tool arguments), pass `match=fn`. `fn(effect_args)` maps the effect's visible arguments to the dict shape passed to `shape.approval(...)` before hashing. The `scope` must still match.

### `requires(op, before=other_op, same_scope=True)`

**Bug it catches:** Out-of-order effects, such as emailing "Your card was charged" when the charge failed before commit or was skipped by a perturbed model.

Every `op` effect must have an earlier `before` effect that **landed** in the world log. By default (`same_scope=True`), both effects must share the same scope; pass `same_scope=False` to accept any scope. A `before` call that timed out after commit counts as landed; a call that timed out before commit does not.

```python
@shape.scenario(invariants=[shape.requires("email.send", before="payments.charge")],
                max_restarts=1)
def receipt_after_failed_charge(run: shape.Run) -> None:
    with contextlib.suppress(Exception):
        charge("1", 500, key="order-1")
    send_receipt("1", "You were charged 500.")
```

Real output:

```text
FAIL docs/guide/examples/invariants.py::receipt_after_failed_charge  seed=33  invariant=requires(email.send, before=payments.charge)
  send_receipt(order='1', text='You were charged 500.') happened with no earlier payments.charge that landed for scope 'order:1'
  minimized to 2 steps (1 of 1 deviations kept, 3 replays):
    incarnation 0
      1. charge(order='1', amount=500, key='order-1')
           timeout before the effect: the caller saw TimeoutError
      2. send_receipt(order='1', text='You were charged 500.')
           effect landed
      finished
```

### `within_scopes(op, allowed)`

**Bug it catches:** Cross-tenant or wrong-target actions when the model hallucinates or substitutes an ID (for example, refunding order 2 while handling a ticket for order 1).

`allowed` is either a collection of scope strings (such as `{"order:1"}`) or a callable `allowed(run: shape.Run)` that returns one at the end of the run (useful when allowed scopes are stored in `run.dir`).

```python
@shape.scenario(invariants=[shape.within_scopes("payments.refund", {"order:1"})],
                max_restarts=1)
def refund_wrong_order(run: shape.Run) -> None:
    refund(shape.choose("which_order", ["1", "2"]), 500)
```

Real output:

```text
FAIL docs/guide/examples/invariants.py::refund_wrong_order  seed=3  invariant=within_scopes(payments.refund)
  refund(order='2', amount=500) acted on 'order:2', outside the allowed scopes ('order:1')
  minimized to 2 steps (1 of 1 deviations kept, 3 replays):
    incarnation 0
      1. model chose '2' at 'which_order' (nominal: '1')
      2. refund(order='2', amount=500)
           effect landed
      finished
```

### `max_calls(op, n, per_scope=True)`

**Bug it catches:** Unbounded retry storms and runaway tool loops that hammer a provider.

Counts **attempts** (not just landed effects), including failed and killed calls. Defaults to `per_scope=True`; pass `per_scope=False` to cap total calls across all scopes.

```python
@shape.scenario(invariants=[shape.max_calls("payments.charge", 3)], max_restarts=1)
def retry_storm(run: shape.Run) -> None:
    for _ in range(10):
        with contextlib.suppress(Exception):
            return charge("1", 500, key="order-1")
```

Real output:

```text
FAIL docs/guide/examples/invariants.py::retry_storm  seed=6  invariant=max_calls(payments.charge, 3)
  payments.charge was called 4 times for scope 'order:1', failed calls included (at most 3 allowed)
  minimized to 4 steps (3 of 3 deviations kept, 9 replays):
    incarnation 0
      1. charge(order='1', amount=500, key='order-1')
           timeout before the effect: the caller saw TimeoutError
      2. charge(order='1', amount=500, key='order-1')
           server error before the effect: the caller saw ServerError
      3. charge(order='1', amount=500, key='order-1')
           timeout before the effect: the caller saw TimeoutError
      4. charge(order='1', amount=500, key='order-1')
           effect landed (idempotency key 'order-1')
      finished
```

### `sum_le(op, field, limit, per_scope=True)`

**Bug it catches:** Over-refunds or budget overruns across multiple partial actions (for example, refunding the item total and then issuing an extra shipping refund that exceeds the order value).

Sums the numeric argument named `field` across all real deduplicated effects of `op` (missing or non-numeric values count as `0`). Defaults to `per_scope=True`.

```python
@shape.scenario(invariants=[shape.sum_le("payments.refund", "amount", 500)], max_restarts=1)
def over_refund(run: shape.Run) -> None:
    refund("1", 500)
    if shape.choose("after_refund", ["reply", "refund_shipping_too"]) == "refund_shipping_too":
        refund("1", 50)
```

Real output:

```text
FAIL docs/guide/examples/invariants.py::over_refund  seed=1  invariant=sum_le(payments.refund.amount, 500)
  payments.refund amount added up to 550 for scope 'order:1' (limit 500)
  minimized to 3 steps (1 of 1 deviations kept, 3 replays):
    incarnation 0
      1. refund(order='1', amount=500)
           effect landed
      2. model chose 'refund_shipping_too' at 'after_refund' (nominal: 'reply')
      3. refund(order='1', amount=50)
           effect landed
      finished
```

### `claims_true()` and `shape.claim(scope, op, ok=True)`

**Bug it catches:** An agent telling the user "I refunded your order" when the refund failed before commit or never ran — or telling the user "I couldn't refund your order" when the refund actually landed (for example after `timeout_after_commit`).

Call `shape.claim(scope, op)` (`ok=True`) when your agent tells the user that `op` happened for `scope`, and `shape.claim(scope, op, ok=False)` when it tells the user `op` could not happen. `shape.claims_true()` checks that every `ok=True` claim has an earlier landed effect and every `ok=False` claim has no earlier landed effect.

```python
@shape.scenario(invariants=[shape.claims_true()], max_restarts=1)
def claim_without_refund(run: shape.Run) -> None:
    with contextlib.suppress(Exception):
        refund("1", 500)
    shape.claim("order:1", "payments.refund")
```

Real output:

```text
FAIL docs/guide/examples/invariants.py::claim_without_refund  seed=31  invariant=claims_true
  the agent told the user payments.refund happened for scope 'order:1', but no such effect had landed
  minimized to 2 steps (1 of 1 deviations kept, 3 replays):
    incarnation 0
      1. refund(order='1', amount=500)
           timeout before the effect: the caller saw TimeoutError
      2. agent told the user payments.refund happened for 'order:1'
      finished
```

### `scenario_terminates()`

**Bug it catches:** Agents that crash-loop forever, leave corrupt persisted state that cannot be resumed, or hang past the 30-second incarnation timeout.

`scenario_terminates()` is always enabled on every scenario. You do not need to add it to `invariants=[...]`. It passes when the scenario returns within `max_restarts` restarts (with the final incarnation running fault-free whenever `max_restarts > 0`).

## When to use `claims_true()` instead of `exactly_once()`

`exactly_once(op)` fails whenever the scenario finishes without at least one landed effect of `op`. That is the right invariant for a background worker or checkout queue handler that must finish the job (or raise so the queue retries it).

User-facing agents are different. When a payment API returns repeated errors or a human rejects a request, the right response from a support agent is often to stop and tell the user "I could not issue the refund right now." Under `exactly_once`, that safe, honest refusal fails as a lost effect.

For user-facing agents that may legitimately give up:

1. Use `shape.at_most_once(op)` + `shape.claims_true()` (calling `shape.claim(scope, op)` on success and `shape.claim(scope, op, ok=False)` on reported failure), **or**
2. Use `shape.exactly_once(op, failure_claim_ok=True)` + `shape.claims_true()`, which requires either a landed effect or an explicit `shape.claim(scope, op, ok=False)` for every attempted scope (still failing if the agent claims success or exits silently).

That allows the agent to apologize and exit cleanly when the provider is down, while still failing any run where the agent lies about an effect or silently drops it.

## Custom invariants with `@shape.invariant`

Decorate a function `fn(world: shape.World) -> str | None` with `@shape.invariant`. Return a one-line message when the run violated your rule, or `None` when it passed. The function name becomes the invariant name. If your invariant function raises an exception, `shape` reports it as a violation (`the invariant itself raised ...`) instead of crashing the sweep.

### `shape.World` reference

| Member | Type / Description |
| --- | --- |
| `world.effects(op=None, scope=None)` | List of real deduplicated effects in log order. Keys: `op`, `scope`, `key`, `call`, `args` (visible arguments), `result` (return value of `result=`), `fault`, `inc`, `twin`. |
| `world.attempts(op=None, scope=None)` | List of every call attempt, including failed and killed calls. Keys: `op`, `scope`, `call`, `fault`, `inc`. |
| `world.approvals` | List of `shape.approval(...)` entries (`scope`, `args_hash`). |
| `world.claims` | List of `shape.claim(...)` entries (`scope`, `op`). |
| `world.completed` | `True` if the scenario returned within `max_restarts`. |
| `world.run` | Final `shape.Run`, so you can inspect files under `world.run.dir`. |

To inspect provider return values in a custom invariant, pass `result=fn` on `@shape.effect`:

```python
@shape.effect(
    "payments.refund",
    scope=lambda order, **_: f"order:{order}",
    result=lambda reply: reply,
)
def refund(order: str, amount: int) -> dict:
    return {"refunded": amount if amount < 1000 else 999}


@shape.invariant
def refund_matches_request(world: shape.World) -> str | None:
    for e in world.effects("payments.refund"):
        if e["result"]["refunded"] != e["args"]["amount"]:
            return f"asked to refund {e['args']['amount']}, provider refunded {e['result']['refunded']}"
    return None
```

Real output from `docs/guide/examples/invariants.py::partial_refund`:

```text
FAIL docs/guide/examples/invariants.py::partial_refund  seed=0  invariant=refund_matches_request
  asked to refund 1500, provider refunded 999
  minimized to 2 steps (1 of 1 deviations kept, 3 replays):
    incarnation 0
      1. model chose 1500 at 'amount' (nominal: 500)
      2. refund(order='1', amount=1500)
           effect landed
      finished
```
