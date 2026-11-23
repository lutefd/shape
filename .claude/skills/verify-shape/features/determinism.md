# determinism

## Sub-features
- Same seeds, same saga, twice: stdout identical except the `wall=...s N runs/s` field, and the written `*.min.json` files byte-identical.

## How to get to it (user POV)
Run `shape run --seeds 200 --saga toy` twice; the failure and its replay file must not change. This is the product's core promise (`README.md`, `docs/design.md`).

## Driving it with verify.sh
`verify.sh determinism` runs the toy saga twice into `det1/` and `det2/`, masks the wall field and the per-run directory path in `det*.masked`, `cmp`s the masked stdout, and `cmp`s each min.json.

## Gotchas
- Only the wall/throughput field and the `--runs-dir` path are masked; any other diff is a real determinism bug, report it rather than widening the mask.
- Runs are in separate processes; in-process determinism is covered by `tests/test_determinism.py`.
