# shrink

## Sub-features
- `shape shrink <file.json>` minimizes the `realized` choices of a failing file, prints the story, writes `<file>.min.json` next to it (`raw.json` -> `raw.min.json`), exit 0. A non-failing file prints `run does not fail; nothing to shrink`, exit 0.

## How to get to it (user POV)
`shape shrink path/to/failing.json`.

## Driving it with verify.sh
`verify.sh shrink` first generates an unshrunk failing file `shrink/raw.json` (toy saga, a seed with >= 5 nonzero choices), then runs the CLI on it and asserts: exit 0, `minimized to N steps`, `raw.min.json` exists, and its choice count is strictly smaller than raw's (observed 5 -> 1).

## Gotchas
- The CLI never emits unshrunk files, so the fixture comes from a `python -I` snippet (documented in SKILL.md). If `run_seed`'s signature changes, fix the snippet, not the assertion.
- Exit 0 even on success of a failing file; look at output and file, not the exit code.
