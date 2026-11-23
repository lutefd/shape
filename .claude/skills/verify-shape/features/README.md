# shape feature map

Harness: `.claude/skills/verify-shape/scripts/verify.sh <subcommand>` driving the installed `shape` CLI. Run `launch` first.

| Feature | Subcommand | File |
| --- | --- | --- |
| run (find + shrink a failure, or pass) | `run-toy`, `run-fixed` | [run.md](run.md) |
| replay a saved failure | `replay` | [replay.md](replay.md) |
| shrink a saved failure | `shrink` | [shrink.md](shrink.md) |
| determinism | `determinism` | [determinism.md](determinism.md) |

Not mapped yet: `shape run --start S` offsets beyond what `run.md` notes, and boundary-mode (third-party framework) runs, which do not exist in the CLI yet (`docs/design.md`).
