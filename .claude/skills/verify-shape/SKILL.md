---
name: verify-shape
description: Drive and prove the `shape` CLI (deterministic simulation testing for agent systems: `shape run|replay|shrink`) from a fresh venv install, capturing transcripts and exit codes as evidence. Use when a change touches src/shape and you need to show the real CLI still finds, shrinks, replays, and reproduces the planted double charge.
---

# verify-shape

Surface: the `shape` console script (`src/shape/cli.py`). No server, no TTY, no ports. Each drive is a plain subprocess, so two checkouts can run side by side (each has its own `runs/` and venv). Seed data: the built-in sagas `toy` (has a planted double charge: retry after `timeout_after_commit` uses a new idempotency key) and `fixed` (correct).

All steps go through one executable helper, run from anywhere inside the checkout:

```sh
.claude/skills/verify-shape/scripts/verify.sh <launch|doctor|run-toy|run-fixed|replay|shrink|determinism|all|cleanup>
```

Each subcommand prints `ok <name>` or `FAIL <name>: ...` and exits nonzero on any FAIL. Python >= 3.12 required.

## Launch

`verify.sh launch` deletes and recreates a throwaway venv at `runs/.verify-venv`, runs `pip install -e '.[dev]'` (the README quickstart), and starts a new evidence dir `runs/verify/<UTC timestamp>/` (path also written to `runs/verify/LATEST`). Ready means `runs/.verify-venv/bin/shape` exists. Every later drive uses that installed entry point, never `python -c` into internals (the one exception is the `shrink` fixture, below). Override the evidence dir with `EVIDENCE_DIR=/abs/path`.

## Doctor

`verify.sh doctor` is read-only and prints `DOCTOR OK ...` or `DOCTOR FAIL: why`. It checks: the venv's `shape` exists and `--help` runs; `import shape` resolves under *this checkout's* `src/shape` (an editable install pointing at another worktree fails here); the installed version; the git HEAD. Run it first whenever anything looks off, and after `launch`.

## Drive

Features and their commands live in [features/](features/README.md). Quick path: `verify.sh all` runs launch, doctor, and every feature; run `cleanup` yourself afterwards. To drive one feature, run `launch` once, then its subcommand.

Exit codes you should see: `run --saga toy` = 1 (failure found), `run --saga fixed` = 0, `replay` of a failing file = 1, `shrink` = 0, an `Escape` from the seal = 2 (a real bug in the sim, never expected).

## Evidence

Everything lands in `runs/verify/<timestamp>/` (gitignored via `runs/`): per drive `<name>.cmd` (exact command), `.out`, `.err`, `.exit`, plus generated files in `run-toy/`, `run-fixed/`, `shrink/`, `det1/`, `det2/` (the `*.min.json` replay files). A proof must show the action and the resulting state: the transcript *and* the file the CLI wrote *and* the exit code. Report the evidence dir path and the key lines (the `FAIL seed=... minimized to N steps` block, `PASS`, exit codes).

Proof standards: drive the installed CLI only; do not assert on internal functions; a `fixed` pass and a `toy` failure are both required, since a pass alone proves nothing. Never edit a saga or invariant to make a check pass. `--runs-dir` is always pointed into the evidence dir, so the CLI's default `runs/` output is not touched.

## Cleanup

`verify.sh cleanup` removes only `runs/.verify-venv`. Evidence in `runs/verify/` is deliberately kept; delete old timestamp dirs by hand when done. Nothing else is left running (no processes are started in the background). After a failed run, run cleanup too, then `launch` again.

## Helpers

- `scripts/verify.sh` (executable, invocation above). The `shrink` subcommand builds its unshrunk failing input with a short `python -I` snippet using `shape.simulation.run_seed`, because the CLI only writes already-minimized files; that snippet is fixture generation, the shrink itself is driven via `shape shrink`.
