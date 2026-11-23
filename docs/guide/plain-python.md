# Testing plain Python agents

If your agent is a plain Python loop, async worker, or custom tool runner with no framework, you only need the core `shape` package (zero dependencies). This guide walks through wiring up `examples/plain_python/` and an `asyncio` example in `docs/guide/examples/async_agent.py`.

For the full API contract, see [docs/boundary.md](../boundary.md).

## 1. Mark the external boundary with `@shape.effect`

Put `@shape.effect` on the client method that crosses into the outside world, not on an outer retry loop or a helper that returns early based on agent state.

In [`examples/plain_python/payments.py`](../../examples/plain_python/payments.py), `PaymentClient.charge` writes to a SQLite database in `run.dir` so committed charges survive `os._exit(137)` crashes, and deduplicates non-null idempotency keys the way a real payment provider does:

```python
class PaymentClient:
    def __init__(self, db_path: Path) -> None:
        self.db = sqlite3.connect(db_path, timeout=30)
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS charges ("
                            "id INTEGER PRIMARY KEY, order_id TEXT, amount INTEGER, "
                            "idempotency_key TEXT UNIQUE)")

    @shape.effect("payments.charge",
                  scope=lambda order_id, **_: f"order:{order_id}",
                  key=lambda idempotency_key, **_: idempotency_key)
    def charge(self, order_id: str, amount: int, idempotency_key: str | None = None) -> int:
        with self.db:
            if idempotency_key is not None:
                row = self.db.execute("SELECT id FROM charges WHERE idempotency_key = ?",
                                      (idempotency_key,)).fetchone()
                if row:
                    return row[0]
            cursor = self.db.execute(
                "INSERT INTO charges (order_id, amount, idempotency_key) VALUES (?, ?, ?)",
                (order_id, amount, idempotency_key))
            return cursor.lastrowid
```

Because `@shape.effect` inspects the function's signature, `scope` and `key` receive every named argument (including `self`, defaults, and `**kwargs`). Outside a `shape` run, `PaymentClient.charge` executes with no interception.

## 2. Persist progress in `run.dir`

When `shape` injects `crash_before` or `crash_after`, it kills the child process with `os._exit(137)` and starts a fresh process on the same `run.dir`. Any state you want to survive a crash must live on disk under `run.dir`.

In [`examples/plain_python/agent.py`](../../examples/plain_python/agent.py), `process_order` stores order progress in `run.dir / "progress.db"` and calls `charge_with_retry`:

```python
def charge_with_retry(client: PaymentClient, order_id: str, amount: int) -> int:
    key = f"charge-{order_id}"
    for _ in range(ATTEMPTS):
        try:
            return client.charge(order_id, amount, idempotency_key=key)
        except (TimeoutError, shape.RateLimited, shape.ServerError):
            continue  # nothing is known to have happened: the keyed retry is safe
        except ConnectionResetError:
            # the connection dropped, so reconnect and go again
            return client.charge(order_id, amount)
    raise PaymentFailed(order_id)
```

Notice the bug: the `ConnectionResetError` branch retries without passing `idempotency_key=key`.

## 3. Write a `@shape.scenario` and run it

In [`examples/plain_python/checks.py`](../../examples/plain_python/checks.py):

```python
CRASH_SAFE = [shape.at_most_once("payments.charge"), shape.exactly_once("payments.charge")]

@shape.scenario(invariants=CRASH_SAFE, max_restarts=3)
def checkout_buggy(run: shape.Run) -> None:
    process_order(run.dir, "o1", 500, charge_with_retry)

@shape.scenario(invariants=CRASH_SAFE, max_restarts=3)
def checkout_fixed(run: shape.Run) -> None:
    process_order(run.dir, "o1", 500, charge_with_retry_fixed)
```

Run `checkout_buggy` for 200 seeds:

```sh
shape test examples/plain_python/checks.py::checkout_buggy --seeds 200
```

Real output:

```text
scenario=checkout_buggy  seeds=200 (from 0)  failing=2  wall=6.12s  33 seeds/s
coverage over 200 seeds:
  op                landed  attempts
  payments.charge      202       252
                   faults injected: client_error 2, connection_reset_after_commit 2, crash_after 11, crash_before 7, rate_limit 4, server_error 5, timeout 9, timeout_after_commit 12
  model choices taken: none
  interleaving choices taken: none
scope assignment:
  payments.charge: explicit scope= (e.g. 'order:o1')
1 defect across 2 failing seeds

defect 1 of 1: at_most_once(payments.charge)  op=payments.charge  scope=order:<id>  (2 of 2 failing seeds)
  reply lost: 2 seeds (connection_reset_after_commit, timeout_after_commit)
  before-commit fault: 1 seed (server_error)
FAIL examples/plain_python/checks.py::checkout_buggy  seed=52  invariant=at_most_once(payments.charge)
  payments.charge happened 2 times for scope 'order:o1' (at most 1 allowed)
  minimized to 3 steps (2 of 3 deviations kept, 6 replays):
    incarnation 0
      1. PaymentClient.charge(order_id='o1', amount=500, idempotency_key='charge-o1')
           effect landed (idempotency key 'charge-o1'), then timeout after commit: the caller saw TimeoutError
      2. PaymentClient.charge(order_id='o1', amount=500, idempotency_key='charge-o1')
           effect landed (idempotency key 'charge-o1'), then connection reset after commit: the caller saw ConnectionResetError
      3. PaymentClient.charge(order_id='o1', amount=500, idempotency_key=None)
           effect landed
      finished
  replay: runs/checkout_buggy/seed-52.min.json
```

### The fix

`charge_with_retry_fixed` folds `ConnectionResetError` into the keyed retry branch so every retry carries `idempotency_key=key`:

```python
def charge_with_retry_fixed(client: PaymentClient, order_id: str, amount: int) -> int:
    key = f"charge-{order_id}"
    for _ in range(ATTEMPTS):
        try:
            return client.charge(order_id, amount, idempotency_key=key)
        except (TimeoutError, ConnectionResetError, shape.RateLimited, shape.ServerError):
            continue  # the charge may have landed; the same key makes the retry safe
    raise PaymentFailed(order_id)
```

Running `checkout_fixed` passes all 200 seeds:

```sh
shape test examples/plain_python/checks.py::checkout_fixed --seeds 200
```

Real output:

```text
scenario=checkout_fixed  seeds=200 (from 0)  failing=0  wall=0.90s  221 seeds/s
coverage over 200 seeds:
  op                landed  attempts
  payments.charge      200       252
                   faults injected: client_error 2, connection_reset_after_commit 2, crash_after 11, crash_before 7, rate_limit 4, server_error 5, timeout 9, timeout_after_commit 12
  model choices taken: none
  interleaving choices taken: none
scope assignment:
  payments.charge: explicit scope= (e.g. 'order:o1')
PASS
```

## 4. Async Python agents

`@shape.effect` works directly on `async def` functions. Faults fire when the coroutine is awaited, and `crash_after` kills the process after the awaited body returns.

[`docs/guide/examples/async_agent.py`](examples/async_agent.py) shows another common bug: minting a fresh idempotency key per attempt (`f"{order_id}-{attempt}"`) instead of deriving it from the order (`f"charge-{order_id}"`):

```python
@shape.effect("payments.charge",
              scope=lambda order_id, **_: f"order:{order_id}",
              key=lambda idempotency_key, **_: idempotency_key)
async def charge(self, order_id: str, amount: int, idempotency_key: str) -> str:
    return await asyncio.to_thread(self._charge, order_id, idempotency_key)
```

Running both scenarios in `docs/guide/examples/async_agent.py`:

```sh
shape test docs/guide/examples/async_agent.py --seeds 200
```

The async example uses [the cron preset](recovery.md). The buggy key produces a single defect across the failing seeds. The stable key passes.

```text
1 defect across 12 failing seeds

defect 1 of 1: at_most_once(payments.charge)  op=payments.charge  scope=order:<id>  (12 of 12 failing seeds)
  reply lost: 11 seeds (connection_reset_after_commit, timeout_after_commit)
  before-commit fault: 4 seeds (rate_limit, server_error, timeout)
  crash after commit: 1 seed (crash_after)
  crash before: 1 seed (crash_before)
FAIL docs/guide/examples/async_agent.py::async_per_attempt_key  seed=7  invariant=at_most_once(payments.charge)
  payments.charge happened 2 times for scope 'order:o1' (at most 1 allowed)
  minimized to 2 steps (1 of 1 deviations kept, 2 replays):
    incarnation 0
      1. Payments.charge(order_id='o1', amount=500, idempotency_key='o1-0')
           effect landed (idempotency key 'o1-0'), then timeout after commit: the caller saw TimeoutError
      2. Payments.charge(order_id='o1', amount=500, idempotency_key='o1-1')
           effect landed (idempotency key 'o1-1')
      finished
  replay: runs/async_per_attempt_key/seed-7.min.json
```

```text
scenario=async_stable_key  seeds=200 (from 0)  failing=0  wall=3.48s  57 seeds/s
coverage over 200 seeds:
  op                landed  attempts
  payments.charge      200       252
                   faults injected: client_error 2, connection_reset_after_commit 2, crash_after 11, crash_before 7, rate_limit 4, server_error 5, timeout 9, timeout_after_commit 12
  model choices taken: none
  interleaving choices taken: none
scope assignment:
  payments.charge: explicit scope= (e.g. 'order:o1')
PASS
```

## 5. Adding model decisions, custom errors, webhooks, and world actors

- **Real exception types**: If your retry loop catches `httpx.ReadTimeout` or `stripe.APIConnectionError` rather than `TimeoutError`, pass `errors={"timeout": lambda: httpx.ReadTimeout("timeout"), ...}` to `@shape.effect`.
- **Tools that return error dicts or refusals**: Pass `landed=lambda res: bool(res.get("ok"))` on `@shape.effect` so calls that return failure payloads log as `returned without effect` instead of landed effects. For tools that dedupe via local state and return an existing resource ID on repeat calls, pass `key_from_result=lambda res: res.get("id")`. Whenever possible, still prefer putting `@shape.effect` on the inner API client method that touches the outside world.
- **Model perturbations**: Wherever your agent reads a decision from a model, replace the LLM with a scripted function that calls `shape.choose(label, [nominal, bad_choice_1, ...])`. See [Concepts](concepts.md#model-choices-shapechoose).
- **Duplicate delivery and concurrent races**: Pass `duplicate_trigger=True` or `concurrent=[other_actor]` on `@shape.scenario` (marking shared-state boundaries with `shape.point(label)`), or use `shape.run_async(coro)` for deterministic `asyncio` task interleavings. See [Concurrency in docs/boundary.md](../boundary.md#concurrency) and [`examples/races/`](../../examples/races/).
- **External state edits while crashed**: Decorate a function with `@shape.actor` and pass `actors=[my_actor]` on `@shape.scenario`. `shape` runs it in the parent process between incarnations so you can mutate files or database rows while the agent is down.

Next steps: read [Reading a trace](reading-a-trace.md) to dissect failures and replay files, or [CI and pytest](ci.md) to wire scenarios into your test suite.
