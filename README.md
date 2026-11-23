# shape

An agent calls a payment API, the charge commits, and a timeout or connection reset hides the reply so the caller retries and charges twice. A worker crashes mid-tool and restarts from a checkpoint or redelivered job without knowing the external action already landed. The model itself is an untrusted input that can repeat a tool call, skip a step, or substitute the wrong order ID.

`shape` tests whether the system around your agent stays safe under those failures. You mark the calls that touch the outside world, name the invariants that must hold, and `shape` injects faults, real `os._exit(137)` crashes, restarts, and model perturbations across hundreds of seeds, shrinking every failure to a minimal replayable trace.

## Who this is for (and who does not need it)

**Use `shape` if** you ship Python agents (plain Python, LangGraph, or OpenAI Agents SDK) that perform real external side effects: payments, refunds, emails, wire transfers, tickets, or third-party API writes.

**You do not need `shape` if:**

- Your agent is read-only (search, RAG, summarization) with no external non-idempotent side effects.
- You are checking nominal-path logic bugs (prompt formatting, math, schema validation) that a unit test already finds.
- Your prototype keeps all state in process memory (real crashes wipe memory; crash recovery requires durable persistence).
- Your workflows run entirely inside Temporal or DBOS: you already get crash-resumable orchestration by construction, though you still need idempotency keys on external activities and authorization checks around model tool calls.

See [FAQ and honest limits](docs/guide/faq.md) for a detailed comparison with pytest, Hypothesis, Toxiproxy, Temporal/DBOS, and Antithesis.

## Install

Requires Python 3.12+. Core `shape` has zero dependencies. It imports as `shape` and installs the `shape` CLI.

```sh
pip install "git+https://github.com/lutefd/shape"
pip install "agent-shape[langgraph] @ git+https://github.com/lutefd/shape"
pip install "agent-shape[agents] @ git+https://github.com/lutefd/shape"
```

`pip install agent-shape` (with `[langgraph]` and `[agents]` extras) will be published to PyPI at release; it is not on PyPI yet.

To run the examples from a clone of this repository:

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

## 5-minute quickstart

[`examples/plain_python/`](examples/plain_python/) is a small checkout agent with no framework: a retry loop around a payment call, progress saved to SQLite, and a restart that resumes from disk.

1. Mark the external client call with `@shape.effect`:

```python
# examples/plain_python/payments.py
@shape.effect("payments.charge",
              scope=lambda order_id, **_: f"order:{order_id}",
              key=lambda idempotency_key, **_: idempotency_key)
def charge(self, order_id: str, amount: int, idempotency_key: str | None = None) -> int: ...
```

2. Write a scenario and name your invariants:

```python
# examples/plain_python/checks.py
CRASH_SAFE = [shape.at_most_once("payments.charge"), shape.exactly_once("payments.charge")]

@shape.scenario(invariants=CRASH_SAFE, max_restarts=3)
def checkout_buggy(run: shape.Run) -> None:
    process_order(run.dir, "o1", 500, charge_with_retry)
```

3. Run 200 seeds. `shape` forks each incarnation, injects timeouts, resets, rate limits, 4xx/5xx errors, and `os._exit(137)` crashes at `charge`, restarts on every death, and checks invariants against a world log written outside the agent process:

```console
$ .venv/bin/shape test examples/plain_python/checks.py::checkout_buggy --seeds 200
scenario=checkout_buggy  seeds=200 (from 0)  failing=2  wall=1.10s  181 seeds/s
coverage over 200 seeds:
  op                landed  attempts
  payments.charge      202       252
                   faults injected: client_error 2, connection_reset_after_commit 2, crash_after 11, crash_before 7, rate_limit 4, server_error 5, timeout 9, timeout_after_commit 12
  model choices taken: none
2 distinct causes across 2 failing seeds

cause 1 of 2: 1 of 2 failing seeds (52)
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

The `except ConnectionResetError:` branch retried without `idempotency_key='charge-o1'` after the charge had already committed. Passing the same key on every retry (`checkout_fixed`) passes all 200 seeds, and the saved `.min.json` replays deterministically:

```sh
.venv/bin/shape test examples/plain_python/checks.py::checkout_fixed --seeds 200   # PASS
.venv/bin/shape test examples/plain_python/checks.py::checkout_buggy --replay runs/checkout_buggy/seed-52.min.json
```

## Framework guides

- **Plain Python (sync and async):** Decorate client methods directly with `@shape.effect`, persist state in `run.dir`, and explore model choices with `shape.choose`. See [Testing plain Python agents](docs/guide/plain-python.md).
- **LangGraph (`shape.langgraph`):** Wrap tools with `effects()`, drive `StateGraph` and `interrupt()` flows with `drive()` / `adrive()`, and perturb plans with `ScriptedChatModel`. Catches missing idempotency keys, wrong-target tool calls, default `durability="async"` checkpoint loss, and `RetryPolicy()` double charges. See [Testing LangGraph agents](docs/guide/langgraph.md).
- **OpenAI Agents SDK (`shape.agents`):** Wrap `FunctionTool`s with `effects()`, run locally with `ScriptedModel` and `drive()` (using `SQLiteSession` and `RunState` persistence, no API key required). Catches model repeats, tool-error retries, mid-tool crashes, and unapproved targets. See [Testing OpenAI Agents SDK agents](docs/guide/openai-agents.md).

## CI and pytest

`shape test` exits `0` on pass, `1` on failure (`FAIL`, `FLAKY`, or `NOTHING TESTED`), and `2` on import errors or diverged replays. Run in parallel with `--jobs 4`:

```sh
shape test examples/plain_python/checks.py::checkout_fixed --seeds 200 --jobs 4 --runs-dir /tmp/shape-runs
```

Or invoke directly from pytest (raising `AssertionError` carrying the minimized trace on failure):

```python
import shape
from checks import checkout_fixed

def test_checkout_is_crash_safe(tmp_path):
    shape.check(checkout_fixed, seeds=200, runs_dir=tmp_path)
```

See [Running `shape` in CI and pytest](docs/guide/ci.md) for a GitHub Actions workflow, seed budgets, exit codes, and regression replay files.

## What `shape` catches and does not catch

| What `shape` catches | What `shape` does not catch |
| --- | --- |
| Double charges, duplicate emails, and over-refunds after timeouts, resets, retries, or crashes (`at_most_once`, `sum_le`). | Bugs in read-only agents or nominal-path business logic without external side effects. |
| Lost effects when progress is recorded before an effect lands (`exactly_once`). | Real provider API mismatches when your test double does not faithfully mirror the provider's idempotency or error rules. |
| Unapproved or tampered irreversible actions (`approved_before`, `@shape.actor`). | Crashes at arbitrary bytecode instructions between `@shape.effect` calls. |
| Out-of-order effects, wrong-target IDs, retry storms, and false user claims (`requires`, `within_scopes`, `max_calls`, `claims_true`). | Thread timing races or wall-clock bugs (reported as `FLAKY` or `NOT MINIMIZED` rather than deterministic traces). |
| Model misbehaviors: repeated calls, skipped calls, swapped order, wrong arguments, and post-restart re-issues (`shape.choose`, scripted models). | Live LLM prompt quality or hallucinations outside the choices modeled in your scripted model. |

## Documentation

### User guide

1. [Core concepts](docs/guide/concepts.md): effects, scopes, keys, faults, incarnations, `shape.choose`, shrinking, coverage, `FLAKY`, `NOTHING TESTED`, and distinct causes.
2. [Invariants](docs/guide/invariants.md): every built-in invariant with runnable examples, `claims_true` vs. `exactly_once`, and custom `@shape.invariant` checks over `shape.World`.
3. [Reading a trace](docs/guide/reading-a-trace.md): line-by-line trace walkthrough, `.min.json` replay files, and a checklist to tell a real bug from a harness artifact.
4. Framework guides: [Plain Python](docs/guide/plain-python.md), [LangGraph](docs/guide/langgraph.md), [OpenAI Agents SDK](docs/guide/openai-agents.md).
5. [CI and pytest](docs/guide/ci.md): GitHub Actions, `--jobs`, exit codes, `shape.check`, and checked-in replay traces.
6. [FAQ and honest limits](docs/guide/faq.md): when `shape` is the wrong tool, current limits, disk usage of `runs/`, and comparisons with other tools.

### Reference

- [Boundary mode contract](docs/boundary.md): complete API specification, execution lifecycle, and fault table.
- [Framework adapters reference](docs/adapters.md): `shape.langgraph` and `shape.agents` signatures, perturbation table, and resume policies.

### Internals and research

- [Core simulation design](docs/design.md): deterministic simulation core (`shape run --seeds 10000 --saga toy`), virtual event loop, sealed runtime, and verified fakes.
- [Jepsen methodology for agent frameworks](docs/jepsen/methodology.md) and [LangGraph claims inventory](docs/jepsen/claims/langgraph.md): version-pinned claim testing against third-party frameworks.
- [TLA+ saga specification](verification/tla/README.md): formal protocol model behind the reference saga runtime.

## License

Apache-2.0. The PyPI distribution name at release is `agent-shape` because `shape` is taken by an unrelated package that also imports as `shape`; do not install both in one environment.

## Releasing

Bump `version` in `pyproject.toml`, merge, then push a matching tag (`git tag v0.1.1 && git push origin v0.1.1`). The `release` workflow builds, smoke-tests the wheel, and uploads to PyPI with the `PYPI_API_TOKEN` repository secret.
