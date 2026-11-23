# FAQ and honest limits

## When `shape` is the wrong tool

Do not add `shape` when any of the following is true:

- **Your agent has no external non-idempotent side effects.** Read-only RAG, search, summarization, and classification agents do not mutate external state. Regular unit tests and evals cover them better.
- **You are looking for nominal-path logic bugs.** Wrong prompt formatting, broken JSON parsing, or off-by-one arithmetic fail on the happy path. A normal pytest test finds those in milliseconds with no fault injection.
- **Your code is a demo or prototype with only in-memory state.** `shape` kills processes with `os._exit(137)`, which wipes memory. Testing crash recovery only makes sense once your agent persists progress or checkpoints to disk or a database.
- **Your workflows run entirely inside Temporal or DBOS.** Durable execution engines already handle workflow-level crash replay by construction. You still need idempotency keys on external activities and authorization checks around model tool calls, so `shape` can still test those boundaries, but you do not need it to prove the workflow engine itself persists steps.

## Honest limits of boundary mode

- **Point-granularity concurrency, not arbitrary OS thread scheduling.** Concurrent actors (`@shape.scenario(concurrent=[...])` and `duplicate_trigger=True`) are deterministically scheduled across processes at `@shape.effect` boundaries and `shape.point(label)` markers, and `shape.run_async(coro)` deterministically schedules `asyncio` callbacks with a virtual timer clock (see [Concurrency in docs/boundary.md](../boundary.md#concurrency) and [`examples/races/`](../../examples/races/)). User-created OS threads and wall-clock timers still run on the host; if a failure depends on host thread timing, a seed shows up as `FLAKY` or `NOT MINIMIZED`.
- **Crashes only happen at `@shape.effect` boundaries.** `shape` injects `crash_before` and `crash_after` around marked effect calls. It does not kill the process at arbitrary Python bytecode instructions between effects.
- **Your test double must honor idempotency keys truthfully.** `shape` counts two calls with the same `(op, scope, key)` as one effect because real providers deduplicate by key. If your real provider rejects that key format, expires keys early, or ignores keys on that endpoint, `shape` will not catch the mismatch. Verify your double's semantics against the real provider sandbox.
- **Fork vs. spawn performance.** On Unix, `shape` imports the scenario once and forks per incarnation. On Windows, or when `SHAPE_START_METHOD=spawn` is set, each incarnation spawns a fresh Python interpreter and re-imports the scenario file, which is slower and requires the scenario file to be safely importable at top level.

## How much disk space does `runs/` use?

By default, `shape test` stores artifacts under `runs/<scenario>/`:

- **Passing seeds** are deleted immediately after they pass (along with any stale `seed-<n>` directory from an earlier failing run, and an empty `runs/<scenario>/` directory when all seeds pass).
- **Failing seeds** keep `runs/<scenario>/seed-<n>/` (world log, realized log, `inc-*.out`, and any SQLite or state files your scenario wrote to `run.dir`).
- **Minimized causes** also keep `seed-<n>.min/` and `seed-<n>.min.json`.
- **Flaky seeds** keep their first failing run directory (`seed-<n>/`); rerun directories are cleaned up.
- **Replays** write to `runs/<scenario>/replay/`.

Add `runs/` to `.gitignore`, and clean all saved run directories with `rm -rf runs/`. In CI, pass `--runs-dir /tmp/shape-runs` (or upload only `*.min.json` on failure). In pytest, pass `runs_dir=tmp_path` to `shape.check(...)` so pytest cleans up automatically.

## Package name and installation

The package imports as `shape` and installs the `shape` CLI. The PyPI distribution name at release will be `agent-shape` (not yet published), because `shape` on PyPI is an unrelated package that also imports as `shape`. Do not install both in the same virtual environment.

Until the PyPI release, install from GitHub:

```sh
pip install "git+https://github.com/lutefd/shape"
pip install "agent-shape[langgraph] @ git+https://github.com/lutefd/shape"
pip install "agent-shape[agents] @ git+https://github.com/lutefd/shape"
```

## How `shape` compares to other tools

| Tool | What it does well | What `shape` adds |
| --- | --- | --- |
| `pytest` + mocks | Fast, explicit tests for known failure cases and nominal logic. | Explores combinations of faults, real process kills (`os._exit(137)`), restarts, and model perturbations automatically across hundreds of seeds, then shrinks failures to minimal traces. |
| Hypothesis stateful testing | Explores random state-machine action sequences in-process and shrinks them. | Kills the real OS process without running `finally` or `atexit`, restarts from real on-disk checkpoints, models after-commit faults, and includes LangGraph and OpenAI Agents adapters. |
| Toxiproxy / network chaos | Injects real TCP resets, latency, and packet drops between services. | Deterministic seed replay and automatic delta-debugging minimization to a 1-minimal trace, plus model perturbations and world-log invariants with no external proxy infrastructure. |
| Temporal / DBOS | Durable execution runtime that replays workflows safely across process crashes. | Testing tool, not a runtime. Works on existing Python, LangGraph, and Agents SDK code without migrating infrastructure, and checks idempotency keys and model authorization boundaries even if you do use a durable engine. |
| Antithesis | Full-system deterministic simulation inside a custom hypervisor for distributed systems. | Lightweight, zero-dependency Python library that runs locally and in standard GitHub Actions in seconds, focused on agent tool boundaries and framework adapters. |
