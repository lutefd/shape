# LangGraph boundary-mode examples

These scenarios run real LangGraph graphs (`StateGraph`, `SqliteSaver`, `RetryPolicy()`, `interrupt()`, `Command(resume=...)`) under `shape` boundary mode with no rewrite of the agent code: only the external provider calls are wrapped with `@shape.effect(...)`, and human approval decisions are recorded with `shape.approval(...)`.

## Setup and run (clean venv)

From the repository root:

```sh
python3 -m venv examples/langgraph/.venv
examples/langgraph/.venv/bin/pip install -e . -r examples/langgraph/requirements.txt
```

Run any file or scenario with one command:

```sh
examples/langgraph/.venv/bin/shape test examples/langgraph/retry_policy.py --seeds 200 --jobs 4
examples/langgraph/.venv/bin/shape test examples/langgraph/durability.py --seeds 200 --jobs 4
examples/langgraph/.venv/bin/shape test examples/langgraph/approval.py --seeds 200 --jobs 4
examples/langgraph/.venv/bin/shape test examples/langgraph/refund_agent.py --seeds 200 --jobs 4
```

Replay any saved minimal counterexample (`.min.json`):

```sh
examples/langgraph/.venv/bin/shape test examples/langgraph/retry_policy.py::retry_connection_reset \
  --replay runs/retry_connection_reset/seed-99.min.json
```

## Versions

Measured on `Linux x86_64`, Python `3.14.7`, with:

- `langgraph==1.2.24`
- `langgraph-checkpoint==4.3.2`
- `langgraph-checkpoint-sqlite==3.1.3`
- `httpx==0.28.1`
- `requests==2.34.2`
- Docs quotes fetched `2026-11-22` (see [`docs/jepsen/claims/langgraph.md`](../../docs/jepsen/claims/langgraph.md)).

## Results (200 seeds, `--start 0 --jobs 4`)

Classification follows [`docs/jepsen/methodology.md`](../../docs/jepsen/methodology.md):

- **(a) / class 1**: contradicts a verbatim quoted doc sentence under documented faults.
- **(b) / class 2**: behaves as documented, but the default config or tutorial pattern violates a workload safety invariant.
- **(c) / class 3**: works as designed (documented at-least-once node execution without idempotency keys).

| File | Scenario | Invariants checked | Result (200 seeds) | Shrunk seed / fault | Class |
| --- | --- | --- | --- | --- | --- |
| `retry_policy.py` | `retry_connection_reset` | `at_most_once(payments.charge)` | **FAIL** (30/200; `at_most_once` + `scenario_terminates` from `max_restarts=0`, 2 defects) | `seed=195` (`connection_reset_after_commit` -> `ConnectionResetError`) | **(a) / 1** (LG-C24) |
| `retry_policy.py` | `retry_httpx_timeout` | `at_most_once(payments.charge)` | **FAIL** (27/200; `at_most_once` + `scenario_terminates` from `max_restarts=0`, 2 defects) | `seed=7` (`timeout_after_commit` -> `httpx.ReadTimeout`) | **(a) / 1** (LG-C24) |
| `retry_policy.py` | `retry_requests_402_before_commit` | `at_most_once(payments.charge)` + no retry on 402 | **FAIL** (3/200; `scenario_terminates` on forbidden retry, 1 defect) | `seed=15` (`server_error` -> `requests.HTTPError` 402)* | **(a) / 1** (LG-C24) |
| `retry_policy.py` | `retry_httpx_402_not_retried` | `at_most_once(payments.charge)` + no retry on 402 | **PASS** (0/200) | — | control (matches docs) |
| `retry_policy.py` | `retry_fixed` | `at_most_once`, `exactly_once` (`payments.charge`) | **PASS** (0/200) | — | mitigated (`durability="sync"` + key) |
| `durability.py` | `durability_async` | `at_most_once`, `exactly_once` (`payments.charge`, `orders.confirm`) | **FAIL** (40/200, 2 defects; plus occasional async checkpoint loss under host load) | `seed=1` (`orders.confirm` after-commit), `seed=39` (`payments.charge` after-commit) | **(b) / 2** (LG-C06 async checkpoint loss) + **(c) / 3** (mid-node after-commit) |
| `durability.py` | `durability_async_with_key` | `at_most_once`, `exactly_once` (`payments.charge`, `orders.confirm`) | **PASS** (0/200) | — | idempotency keys deduplicate re-runs |
| `durability.py` | `durability_sync_no_key` | `at_most_once`, `exactly_once` (`payments.charge`, `orders.confirm`) | **FAIL** (40/200, 2 defects) | `seed=1` (`orders.confirm` after-commit), `seed=39` (`payments.charge` after-commit) | **(c) / 3** (LG-C23) |
| `durability.py` | `durability_sync_fixed` | `at_most_once`, `exactly_once` (`payments.charge`, `orders.confirm`) | **PASS** (0/200) | — | mitigated (`durability="sync"` + keys) |
| `approval.py` | `approval_gate_only` | `approved_before(transfers.send)` | **PASS** (0/200) | — | holds |
| `approval.py` | `approval_default` | `approved_before`, `at_most_once`, `exactly_once` (`transfers.send`) | **FAIL** (21/200 on `at_most_once`, 1 defect; 0/200 on `approved_before`) | `seed=37` (`transfers.send` after-commit) | **(c) / 3** (LG-C23; `approved_before` holds) |
| `approval.py` | `approval_sync_fixed` | `approved_before`, `at_most_once`, `exactly_once` (`transfers.send`) | **PASS** (0/200) | — | mitigated (`durability="sync"` + key) |
| `approval.py` | `approval_rejected` | `approved_before(transfers.send)` | **PASS** (0/200; 0 transfers) | — | holds |
| `refund_agent.py` | `refund_repeat_unguarded` | `at_most_once(payments.refund)` | **FAIL** (37/200, 1 defect) | `seed=4` (model `'repeat'`), `seed=7` (after-commit), `seed=82` (`crash_before` + `'reissue'`) | **(b) / 2** (unkeyed tool) |
| `refund_agent.py` | `refund_repeat_guarded` | `at_most_once(payments.refund)` | **PASS** (0/200) | — | mitigated (stable key) |
| `refund_agent.py` | `refund_wrong_order_unguarded` | `at_most_once`, `approved_before` (`payments.refund`) | **FAIL** (11/200, 1 defect) | `seed=50` (model `substitute order_id='18'`) | **(b) / 2** (missing allowlist) |
| `refund_agent.py` | `refund_wrong_order_guarded` | `at_most_once`, `approved_before` (`payments.refund`) | **PASS** (0/200) | — | mitigated (allowlist guard) |

*\*Note on the 402 fault mapping:* `retry_requests_402_before_commit` maps `server_error` (a before-commit fault) to a `requests.HTTPError` carrying a status-402 response, and the other non-crash faults (including `client_error`) to `httpx.HTTPStatusError(402)`, so only a retry of the `requests` 402 trips the check. A 402 after the charge committed is not modeled: a declined card does not charge, so that combination would be an invented fault.

## Finding details: `RetryPolicy()` vs documentation (LG-C24, class (a) / 1)

### Quoted documentation (`fault-tolerance`, fetched `2026-11-22`)

From `https://docs.langchain.com/oss/python/langgraph/fault-tolerance`:

> By default, `retry_on` uses `default_retry_on`, which retries on **any** exception except the following (and their subclasses):

followed by an exclusion list ending in `OSError`, and then:

> For exceptions from popular HTTP libraries such as `requests` and `httpx`, it only retries on 5xx status codes.

### Actual code in `langgraph==1.2.24` (`langgraph/_internal/_retry.py`)

```python
def default_retry_on(exc: Exception) -> bool:
    import httpx
    import requests

    if isinstance(exc, ConnectionError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    if isinstance(exc, requests.HTTPError):
        return 500 <= exc.response.status_code < 600 if exc.response else True
    if isinstance(
        exc,
        (
            ValueError,
            TypeError,
            ArithmeticError,
            ImportError,
            LookupError,
            NameError,
            SyntaxError,
            RuntimeError,
            ReferenceError,
            StopIteration,
            StopAsyncIteration,
            OSError,
        ),
    ):
        return False
    return True
```

Three contradictions between the docs and `default_retry_on`:

1. **`ConnectionResetError` (and every `ConnectionError` subclass)**: `ConnectionError` is a subclass of `OSError`, yet `if isinstance(exc, ConnectionError): return True` runs *before* the `OSError` exclusion list.
2. **`requests.HTTPError` with 4xx status (e.g. 402, 409)**: `requests.Response.__bool__` returns `self.ok` (`False` for any status `>= 400`). The truthiness check `if exc.response else True` therefore treats every 4xx and 5xx `requests.Response` as falsy and returns `True` (it should be `if exc.response is not None else True`).
3. **`httpx.ReadTimeout` (and other `httpx.TransportError` subclasses)**: not an `httpx.HTTPStatusError` and not an `OSError`, so it falls through to the final `return True` instead of only retrying 5xx status codes.

In contrast, `httpx.HTTPStatusError(402)` returns `False` (`retry_httpx_402_not_retried` passes 200/200 seeds).

## Minimized traces

Every trace below was produced by `shape test` and verified to reproduce identically with `shape test ... --replay <seed.min.json>` (0 divergences).

### `retry_connection_reset` (`seed=99`, class (a) / 1)

```text
FAIL examples/langgraph/retry_policy.py::retry_connection_reset  seed=99  invariant=at_most_once(payments.charge)
  payments.charge happened 2 times for scope 'order:ord-17' (at most 1 allowed)
  minimized to 2 steps (1 of 1 faults kept, 3 replays):
    incarnation 0
      1. StdlibPaymentClient.charge(order_id='ord-17', amount=5000, idempotency_key=None)
           effect landed, then connection reset after commit: the caller saw ConnectionResetError
      2. StdlibPaymentClient.charge(order_id='ord-17', amount=5000, idempotency_key=None)
           effect landed
      finished
```

### `retry_httpx_timeout` (`seed=7`, class (a) / 1)

```text
FAIL examples/langgraph/retry_policy.py::retry_httpx_timeout  seed=7  invariant=at_most_once(payments.charge)
  payments.charge happened 2 times for scope 'order:ord-17' (at most 1 allowed)
  minimized to 2 steps (1 of 1 faults kept, 3 replays):
    incarnation 0
      1. HttpxPaymentClient.charge(order_id='ord-17', amount=5000, idempotency_key=None)
           effect landed, then timeout after commit: the caller saw ReadTimeout
      2. HttpxPaymentClient.charge(order_id='ord-17', amount=5000, idempotency_key=None)
           effect landed
      finished
```

### `retry_requests_402_before_commit` (`seed=13`, class (a) / 1)

```text
FAIL examples/langgraph/retry_policy.py::retry_requests_402_before_commit  seed=13  invariant=scenario_terminates
  the scenario did not finish within max_restarts=3 (4 incarnations); last error: RuntimeError: LangGraph retried charge_node after a 402 requests.HTTPError
  minimized to 1 steps (1 of 1 faults kept, 3 replays):
    incarnation 0
      1. Requests402BeforeCommitClient.charge(order_id='ord-17', amount=5000, idempotency_key='order-17:charge')
           server error before the effect: the caller saw HTTPError
      process raised: During task with name 'charge' and id '...'
    incarnation 1 (restarted)
      process raised: RuntimeError: LangGraph retried charge_node after a 402 requests.HTTPError
```

### `durability_async` and `durability_sync_no_key` (`seed=39` and `seed=1`)

Mid-node `crash_after` on `charge` (re-runs `charge` on resume without an idempotency key — class (c) / 3 in both `async` and `sync` without keys):

```text
FAIL examples/langgraph/durability.py::durability_async  seed=39  invariant=at_most_once(payments.charge)
  payments.charge happened 2 times for scope 'order:ord-42' (at most 1 allowed)
  minimized to 3 steps (1 of 1 faults kept, 3 replays):
    incarnation 0
      1. CheckoutClient.charge(order_id='ord-42', amount=5000, idempotency_key=None)
           effect landed, then process killed after the effect landed, before the caller saw it
      -> process died
    incarnation 1 (restarted)
      2. CheckoutClient.charge(order_id='ord-42', amount=5000, idempotency_key=None)
           effect landed
      3. CheckoutClient.confirm(order_id='ord-42', charge_id=2, idempotency_key=None)
           effect landed
      finished
```

Under `durability="async"`, `durability_async` additionally failed 3 extra seeds under `--jobs 4` (`seed=90`, `seed=100`, `seed=103`; 41/200 vs 38/200 in `durability_sync_no_key`) where `charge` had already returned and `crash_before` killed `confirm` before the background checkpoint thread committed `charge`'s super-step to `ckpt.db`, causing `graph.invoke(None, config)` to re-run `charge` (class (b) / 2, LG-C06). Because that window is a thread-scheduling race with `BackgroundExecutor`, its hit rate depends on disk/CPU load; `durability_sync_fixed` (`durability="sync"` + idempotency keys) passed 200/200 seeds.

### `approval_default` (`seed=37`, class (c) / 3; `approved_before` holds)

`approved_before(transfers.send)` passed 200/200 seeds across every approval scenario (`approval_gate_only`, `approval_default`, `approval_sync_fixed`, `approval_rejected`): LangGraph's `interrupt()` gate never executed `send` without a prior recorded approval or after rejection. Adding `at_most_once(transfers.send)` to the unkeyed default scenario (`approval_default`) caught the expected at-least-once replay when a fault hits after `send` commits:

```text
FAIL examples/langgraph/approval.py::approval_default  seed=37  invariant=at_most_once(transfers.send)
  transfers.send happened 2 times for scope 'order:ord-7' (at most 1 allowed)
  minimized to 3 steps (1 of 1 faults kept, 3 replays):
    incarnation 0
      1. approval recorded for 'order:ord-7'
      2. TransferClient.send(order_id='ord-7', to='acct-42', amount=50000, idempotency_key=None)
           effect landed, then connection reset after commit: the caller saw ConnectionResetError
      process raised: During task with name 'send' and id '...'
    incarnation 1 (restarted)
      3. TransferClient.send(order_id='ord-7', to='acct-42', amount=50000, idempotency_key=None)
           effect landed
      finished
```
