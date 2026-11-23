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
        run: shape test tests/shape_checks.py --seeds 300 --jobs 4 --strict-scopes --runs-dir /tmp/shape-runs
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

`--jobs J` forks workers on Unix. On platforms without `os.fork`, shape runs seeds serially. With a fixed seed set and no time budget, both modes inspect the same seeds.

Use `--budget 30 --max-shrinks 10` to limit time and shrinking in a PR job. The budget covers seed execution, failure confirmation, and shrinking. Shape reports skipped seeds, confirmations, and shrinks. An incomplete seed sweep exits 1. `--max-shrinks 0` keeps original failing runs and skips minimization. Pass `--strict-scopes` (or `@shape.scenario(..., strict_scopes=True)` / `shape.check(..., strict_scopes=True)`) in CI so duplicate-sensitive invariants refuse default/derived scopes instead of letting unrelated jobs collide into false duplicates.

## JSON sweep reports

Use `shape test examples/plain_python/checks.py::checkout_buggy --seeds 200 --json > shape-report.json` to save a report for a CI annotation or artifact step. The command writes one JSON document to stdout, with a `scenarios` array, and uses the same exit codes as a text sweep. For each scenario, read `defects` for the invariant, operation, scope pattern, failing seed IDs, and representative replay path. Trigger objects include their own seed IDs, fault names, and replay paths. A seed can appear under several triggers.

This projection of the checkout report shows how the replay paths relate to the seed IDs:

```json
{
  "scope_pattern": "order:<id>",
  "seeds": [
    52,
    195
  ],
  "replay_path": "runs/checkout_buggy/seed-52.min.json",
  "triggers": [
    {
      "class": "reply_lost",
      "seeds": [
        52,
        195
      ],
      "replay_path": "runs/checkout_buggy/seed-52.min.json"
    },
    {
      "class": "before_commit",
      "seeds": [
        195
      ],
      "replay_path": "runs/checkout_buggy/seed-195.min.json"
    }
  ]
}
```

When a budget or shrink limit prevents minimization, the corresponding seed IDs appear in `shrink_budget_skipped_seeds` or `shrink_limit_skipped_seeds`. A defect with no verified replay has a null `replay_path`. Each defect and trigger object also includes `trigger_label` (all kept deviations in order), and `flaky_details` lists each flaky seed's saved `runs/<scenario>/flaky/seed-<n>` directory and first divergence. Upload the runs directory along with the JSON to make the replay paths available after the job.

Scenarios with a [Postgres fixture](world-fixtures.md#postgres) need a database in the job. Add a service container and set `SHAPE_PG_DSN`; without it `shape test` prints `SKIP <scenario>: fixture 'pg' is unavailable` for those scenarios and exits 0, and `shape.check` raises `shape.FixtureUnavailable`, which pytest reports as skipped:

```yaml
    services:
      postgres:
        image: postgres:17
        env: { POSTGRES_PASSWORD: shape }
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U postgres" --health-interval 5s --health-retries 10
    env:
      SHAPE_PG_DSN: postgresql://postgres:shape@127.0.0.1:5432/postgres
```

## Exit codes

### Sweep mode (`shape test target --seeds N`)

| Exit code | Meaning |
| --- | --- |
| `0` | Every selected scenario passed with zero failures, zero flaky seeds, and at least one effect called (unless `expect_effects=False`). |
| `1` | At least one scenario failed an invariant, reported a `FLAKY` seed, or reported `NOTHING TESTED` or `INCOMPLETE`. |
| `2` | Target file missing, syntax or import error in the scenario module, or named scenario not found. Set `SHAPE_TRACEBACK=1` to see the full import traceback. |
| `141` | Stdout pipe closed early (for example `shape test ... \| head -1`). Prints nothing to stderr. |

### Replay mode (`shape test target --replay file.min.json`)

| Exit code | Meaning |
| --- | --- |
| `0` | `PASS`: every recorded deviation was reached and the recorded defect no longer reproduces. |
| `1` | `FAIL`: the recorded defect reproduces on replay. |
| `2` | `DIVERGED`: the code no longer reached every recorded deviation key in `realized` (for example after a refactor changed the call structure). |

## Running from `pytest`

Call `shape.check(scenario, seeds=200, start=0, runs_dir=tmp_path, jobs=1)` inside a regular pytest function. Pass `jobs=4` for parallel forked execution on Unix. The matching limits are `budget=30` and `max_shrinks=10`. When the sweep fails or is incomplete, `shape.check` raises `AssertionError` with coverage and the findings it recorded.

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

Passing seed directories are deleted immediately after each seed passes (along with any stale `seed-<n>` or `flaky/seed-<n>` directory from an earlier failing run, and empty scenario directories). Failing seeds, representative `.min` directories and `.min.json` files (one per `(defect, trigger class)`), flaky rerun directories (`flaky/seed-<n>/run-<k>/`), and `replay/` stay under `--runs-dir` (default `runs/`). Add `runs/` to `.gitignore`, clean with `rm -rf runs/`, and use `--runs-dir /tmp/shape-runs` in CI or `runs_dir=tmp_path` in pytest.
