# shape

Test your agent's crash safety. shape injects faults and real process crashes at the points where your agent touches the outside world, records what really happened outside the agent process, and shrinks any failure to a few steps you can read.

The model is an untrusted input. shape asks whether the system around it keeps its safety properties under any sequence of agent decisions and world failures. Everything that varies in a run flows from one seed, so every failure replays exactly.

## Test your agent in 5 minutes

You mark the functions with side effects, write a scenario that runs your agent, and name what must hold.

```sh
pip install agent-shape        # Python 3.12+, no dependencies. Imports as `shape`, CLI is `shape`.
```

To run the examples below from a clone: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`.

`examples/plain_python/` is a small agent with no framework: a retry loop around a payment call, progress saved to a sqlite file, and a restart that resumes from it. The charge call is marked:

```python
# examples/plain_python/payments.py
@shape.effect("payments.charge",
              scope=lambda order_id, **_: f"order:{order_id}",
              key=lambda idempotency_key, **_: idempotency_key)
def charge(self, order_id, amount, idempotency_key=None): ...
```

```python
# examples/plain_python/checks.py
@shape.scenario(invariants=[shape.at_most_once("payments.charge"),
                            shape.exactly_once("payments.charge")], max_restarts=3)
def checkout_buggy(run: shape.Run) -> None:
    process_order(run.dir, "o1", 500, charge_with_retry)
```

Run it. shape forks your scenario, injects timeouts, resets, rate limits and `os._exit` crashes at the `charge` boundary, restarts the process after every death, and checks the invariants against a log of real effects:

```console
$ .venv/bin/shape test examples/plain_python/checks.py::checkout_buggy --seeds 200
scenario=checkout_buggy  seeds=200 (from 0)  failing=4  wall=2.7s  74 seeds/s
FAIL examples/plain_python/checks.py::checkout_buggy  seed=99  invariant=at_most_once(payments.charge)
  payments.charge happened 2 times for scope 'order:o1' (at most 1 allowed)
  minimized to 2 steps (1 of 1 faults kept, 3 replays):
    incarnation 0
      1. PaymentClient.charge(order_id='o1', amount=500, idempotency_key='charge-o1')
           effect landed (idempotency key 'charge-o1'), then connection reset after commit: the caller saw ConnectionResetError
      2. PaymentClient.charge(order_id='o1', amount=500, idempotency_key=None)
           effect landed
      finished
  replay: runs/checkout_buggy/seed-99.min.json
```

The hand-rolled retry catches `ConnectionResetError` after the charge committed and charges again without the idempotency key. The fixed loop retries with the same key, and passes:

```sh
.venv/bin/shape test examples/plain_python/checks.py::checkout_fixed --seeds 200   # PASS
.venv/bin/shape test examples/plain_python/checks.py::checkout_buggy --replay runs/checkout_buggy/seed-99.min.json
```

Failures exit nonzero, so this runs in CI as it is. In pytest:

```python
import shape
from checks import checkout_fixed

def test_checkout_is_crash_safe():
    shape.check(checkout_fixed, seeds=200)   # AssertionError carries the minimized trace
```

The full contract (API, how one seed runs, the fault table, and what shape does not control) is in [docs/boundary.md](docs/boundary.md).

## How shape tests its own reference runtime

shape also ships a deterministic simulation of a toy saga runtime, with simulated payments, email, approvals and a virtual clock. It is the system shape's core was built against, and it plants the same kind of double charge.

See [docs/design.md](docs/design.md).

```sh
.venv/bin/pytest
.venv/bin/shape run --seeds 10000 --saga toy    # finds the planted double charge, shrinks it
.venv/bin/shape run --seeds 10000 --saga fixed  # passes
.venv/bin/shape replay runs/seed-24.at_most_once_effect.min.json
```

## License

Apache-2.0. The PyPI distribution is `agent-shape` because `shape` is taken by an unrelated package that also imports as `shape`; do not install both in one environment.

## Releasing

Bump `version` in `pyproject.toml`, merge, then push a matching tag (`git tag v0.1.1 && git push origin v0.1.1`). The `release` workflow builds, smoke-tests the wheel, and uploads to PyPI with the `PYPI_API_TOKEN` repository secret.
