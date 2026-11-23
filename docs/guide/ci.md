# Running `shape` in CI and pytest

`shape` is designed to run in standard CI runners with no API keys, no network calls, and deterministic exit codes.

## GitHub Actions workflow

A minimal workflow that runs your scenarios in parallel, uses a temporary runs directory, and uploads any minimized `.min.json` traces on failure:

```yaml
name: shape-crash-safety
on:
  pull_request:
  push:
    branches: [main]

jobs:
  shape:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install "git+https://github.com/lutefd/shape"
      - name: Run crash-safety scenarios
        run: shape test tests/shape_checks.py --seeds 300 --jobs 4 --runs-dir /tmp/shape-runs
      - name: Upload minimized traces on failure
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: shape-min-traces
          path: /tmp/shape-runs/**/*.min.json
          if-no-files-found: ignore
```

For LangGraph or OpenAI Agents SDK scenarios, install the matching extra (`agent-shape[langgraph] @ git+https://github.com/lutefd/shape` or `agent-shape[agents] @ git+https://github.com/lutefd/shape`).

## Seeds, wall-clock time, and `--jobs`

Typical throughput on a 4-core runner:

- Plain Python scenarios: ~100 to 200 seeds/s. 200 seeds finish in 1 to 3 seconds.
- LangGraph and OpenAI Agents SDK scenarios: ~25 to 75 seeds/s with `--jobs 4`. 200 to 300 seeds finish in 3 to 15 seconds.

Practical defaults:

- **On every pull request:** `--seeds 200 --jobs 4` (or `300` for agents with many tool calls and model perturbations). Always start from `--start 0` so PR runs are deterministic and comparable.
- **On a nightly schedule:** `--seeds 2000 --jobs 4` with a rotating `--start` (for example `--start $(( $(date +%j) * 2000 ))`) so each night explores fresh seeds.

`--jobs J` forks `J` worker processes on Unix and produces the exact same report as `--jobs 1`. On platforms without `os.fork`, `shape` falls back to serial execution automatically.

## Exit codes

### Sweep mode (`shape test target --seeds N`)

| Exit code | Meaning |
| --- | --- |
| `0` | Every selected scenario passed with zero failures, zero flaky seeds, and at least one effect called (unless `expect_effects=False`). |
| `1` | At least one scenario failed an invariant, reported a `FLAKY` seed, or failed with `NOTHING TESTED`. |
| `2` | Target file missing, syntax or import error in the scenario module, or named scenario not found. Set `SHAPE_TRACEBACK=1` to see the full import traceback. |
| `141` | Stdout pipe closed early (for example `shape test ... \| head -1`). Prints nothing to stderr. |

### Replay mode (`shape test target --replay file.min.json`)

| Exit code | Meaning |
| --- | --- |
| `0` | `PASS`: every recorded deviation was reached and the recorded invariant no longer fails. |
| `1` | `FAIL`: the recorded invariant still fails on replay. |
| `2` | `DIVERGED`: the code no longer reached every recorded deviation key in `realized` (for example after a refactor changed the call structure). |

## Running from `pytest`

Call `shape.check(scenario, seeds=200, start=0, runs_dir=tmp_path, jobs=1)` inside a regular pytest function. Pass `jobs=4` for parallel forked execution on Unix. When a seed fails, is flaky, or hits `NOTHING TESTED`, `shape.check` raises `AssertionError` containing the coverage table and minimized traces.

[`docs/guide/examples/test_async_agent.py`](examples/test_async_agent.py):

```python
import pytest
from async_agent import async_per_attempt_key, async_stable_key

import shape


def test_stable_key_charges_once(tmp_path):
    shape.check(async_stable_key, seeds=200, runs_dir=tmp_path)


def test_per_attempt_key_double_charges(tmp_path):
    with pytest.raises(AssertionError, match=r"at_most_once\(payments.charge\)"):
        shape.check(async_per_attempt_key, seeds=200, runs_dir=tmp_path)
```

Passing `runs_dir=tmp_path` keeps your working tree clean and lets pytest delete failure directories automatically.

## Keeping `.min.json` files as regression tests

When `shape` finds a real bug, copy `runs/<scenario>/seed-<n>.min.json` into your repository (for example `tests/traces/refund-crash-after.min.json`) and replay it in CI after you ship the fix:

```sh
shape test checks.py::refund_agent --replay tests/traces/refund-crash-after.min.json
```

Because `--replay` runs a single explicit execution instead of a full sweep, it completes in milliseconds and guarantees that exact failure schedule stays fixed:

- Exit `0`: the bug is still fixed and the code still reaches the fault point.
- Exit `1`: the bug regressed.
- Exit `2`: the code refactored past the old call sequence; regenerate or retire the trace.

You can also run a checked-in trace directly from pytest using `shape.replay(scenario, path, runs_dir=tmp_path)`, which returns `None` on pass and raises `AssertionError` if the bug regresses or the trace diverges:

```python
import shape

def test_refund_crash_regression(tmp_path):
    shape.replay(
        refund_agent,
        "tests/traces/refund-crash-after.min.json",
        runs_dir=tmp_path,
    )
```

## Disk usage of `runs/`

Passing seed directories are deleted immediately after each seed passes (along with any stale `seed-<n>` directory from an earlier failing run, and empty scenario directories). Failing seeds, `.min` directories, flaky seeds, and `replay/` stay under `--runs-dir` (default `runs/`). Add `runs/` to `.gitignore`, clean with `rm -rf runs/`, and use `--runs-dir /tmp/shape-runs` in CI or `runs_dir=tmp_path` in pytest.
