# run

## Sub-features
- `--saga toy`: finds the planted double charge (`at_most_once_effect`), shrinks the first failing seed of each invariant, writes `<runs-dir>/seed-<N>.<invariant>.min.json`, exits 1.
- `--saga fixed`: all seeds pass, prints `PASS`, writes nothing, exits 0.
- `--seeds N` (default 1000), `--start S` (first seed), `--runs-dir D` (default `runs`).

## How to get to it (user POV)
`shape run --seeds 10000 --saga toy` from a shell (README).

## Driving it with verify.sh
`verify.sh run-toy` and `verify.sh run-fixed` (200 seeds each). Expected toy transcript: `saga=toy seeds=200 ... failing=6`, `FAIL seed=24 invariant=at_most_once_effect`, `minimized to 4 steps`, the story (call, `timeout_after_commit` fault `[documented]`, retry with a new key, 2 charge effects), exit 1. Expected fixed: `PASS`, exit 0, empty `run-fixed/`. To test `--start`, run by hand: `runs/.verify-venv/bin/shape run --seeds 1 --start 24 --saga toy --runs-dir <evidence>/x` should fail on that single seed.

## Gotchas
- Nonzero exit is the *success* signal for toy; the script asserts exit 1 explicitly.
- Seed 24 and `failing=6` are properties of the current sim; if a change moves them, check `docs/design.md` before updating the numbers. The script asserts only the invariant name and the presence of a minimized story.
- The wall-time line varies per run; never diff it.
