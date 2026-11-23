#!/usr/bin/env bash
# Drive the installed `shape` CLI and capture evidence. See ../SKILL.md.
# Usage: verify.sh <launch|doctor|run-toy|run-fixed|replay|shrink|determinism|all|cleanup>
set -u
ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
VENV="$ROOT/runs/.verify-venv"          # gitignored (runs/); removed by cleanup
SHAPE="$VENV/bin/shape"
LATEST="$ROOT/runs/verify/LATEST"       # one-line pointer to the newest evidence dir

evidence_dir() {
  if [ -n "${EVIDENCE_DIR:-}" ]; then echo "$EVIDENCE_DIR"
  elif [ -f "$LATEST" ]; then cat "$LATEST"
  else echo "no evidence dir yet: run 'verify.sh launch' first" >&2; return 1; fi
}

# rec <name> <cmd...>: run, save cmd/stdout/stderr/exit under $EV/<name>.*, echo exit code.
rec() {
  local name=$1; shift
  printf '%s\n' "$*" > "$EV/$name.cmd"
  "$@" > "$EV/$name.out" 2> "$EV/$name.err"; local rc=$?
  echo "$rc" > "$EV/$name.exit"
  echo "[$name] exit=$rc"
  return 0
}

# expect <name> <want-exit> [grep-pattern-in-out]: assert on a recorded transcript.
expect() {
  local name=$1 want=$2 pat=${3:-} got; got=$(cat "$EV/$name.exit")
  if [ "$got" != "$want" ]; then echo "FAIL $name: exit $got, wanted $want"; FAILED=1; return; fi
  if [ -n "$pat" ] && ! grep -Eq "$pat" "$EV/$name.out"; then
    echo "FAIL $name: output missing /$pat/"; FAILED=1; return; fi
  echo "ok   $name"
}

cmd=${1:-}; FAILED=0
case "$cmd" in
launch)
  EV="${EVIDENCE_DIR:-$ROOT/runs/verify/$(date -u +%Y%m%dT%H%M%SZ)}"
  mkdir -p "$EV"; echo "$EV" > "$LATEST"; export EV
  rm -rf "$VENV"
  rec launch-venv python3 -m venv "$VENV"
  rec launch-install "$VENV/bin/pip" install -e "$ROOT[dev]"
  expect launch-venv 0; expect launch-install 0
  [ -x "$SHAPE" ] || { echo "FAIL: $SHAPE missing"; FAILED=1; }
  echo "evidence: $EV"
  ;;
doctor)
  # Read-only: venv present, entry point resolves to this checkout's src, version.
  [ -x "$SHAPE" ] || { echo "DOCTOR FAIL: no $SHAPE (run launch)"; exit 1; }
  loc=$("$VENV/bin/python" -I -c 'import shape,sys;print(shape.__file__)')
  case "$loc" in "$ROOT"/src/shape/*) ;; *) echo "DOCTOR FAIL: shape imported from $loc, not $ROOT/src"; exit 1;; esac
  ver=$("$VENV/bin/pip" show shape | sed -n 's/^Version: //p')
  "$SHAPE" --help > /dev/null || { echo "DOCTOR FAIL: shape --help"; exit 1; }
  echo "DOCTOR OK  shape=$SHAPE  version=$ver  src=$loc  git=$(git -C "$ROOT" rev-parse --short HEAD)"
  ;;
run-toy)
  EV=$(evidence_dir) || exit 1; export EV; mkdir -p "$EV/run-toy"
  rec run-toy "$SHAPE" run --seeds 200 --saga toy --runs-dir "$EV/run-toy"
  expect run-toy 1 'FAIL seed=.*at_most_once_effect'
  grep -E 'minimized to [0-9]+ steps' "$EV/run-toy.out" || FAILED=1
  ls "$EV"/run-toy/*.min.json > /dev/null 2>&1 || { echo "FAIL: no min.json written"; FAILED=1; }
  ;;
run-fixed)
  EV=$(evidence_dir) || exit 1; export EV; mkdir -p "$EV/run-fixed"
  rec run-fixed "$SHAPE" run --seeds 200 --saga fixed --runs-dir "$EV/run-fixed"
  expect run-fixed 0 '^PASS$'
  [ -z "$(ls "$EV/run-fixed" 2>/dev/null)" ] || { echo "FAIL: fixed saga wrote replay files"; FAILED=1; }
  ;;
replay)
  EV=$(evidence_dir) || exit 1; export EV
  min=$(ls "$EV"/run-toy/*.min.json 2>/dev/null | head -1)
  [ -n "$min" ] || { echo "run 'run-toy' first"; exit 1; }
  rec replay "$SHAPE" replay "$min"
  expect replay 1 'VIOLATION at_most_once_effect'
  ;;
shrink)
  EV=$(evidence_dir) || exit 1; export EV; mkdir -p "$EV/shrink"
  # Fixture only (an UNshrunk failing run); the drive below is the CLI.
  "$VENV/bin/python" -I - "$EV/shrink/raw.json" <<'PY'
import json, sys
from shape.simulation import run_seed
for seed in range(500):
    r = run_seed("toy", seed)
    if not r.ok and len(r.realized) >= 5:
        v = r.violations[0].invariant
        json.dump({"saga": "toy", "invariant": v, "seed": seed, "realized": dict(r.realized)},
                  open(sys.argv[1], "w"), indent=2); break
else: sys.exit("no unshrunk fixture found")
PY
  rec shrink "$SHAPE" shrink "$EV/shrink/raw.json"
  expect shrink 0 'minimized to [0-9]+ steps'
  [ -f "$EV/shrink/raw.min.json" ] || { echo "FAIL: raw.min.json missing"; FAILED=1; }
  python3 -c 'import json,sys;a,b=[len(json.load(open(f))["realized"]) for f in sys.argv[1:]];print("choices raw=%d min=%d"%(a,b));sys.exit(0 if b<a else 1)' \
    "$EV/shrink/raw.json" "$EV/shrink/raw.min.json" || { echo "FAIL: shrink did not reduce"; FAILED=1; }
  ;;
determinism)
  EV=$(evidence_dir) || exit 1; export EV
  for i in 1 2; do
    mkdir -p "$EV/det$i"
    rec det$i "$SHAPE" run --seeds 200 --saga toy --runs-dir "$EV/det$i"
    # wall time and throughput are the only nondeterministic fields; mask them
    sed -E 's/wall=[0-9.]+s +[0-9]+ runs\/s/wall=MASKED/; s#'"$EV"'/det[12]#RUNSDIR#' "$EV/det$i.out" > "$EV/det$i.masked"
  done
  cmp "$EV/det1.masked" "$EV/det2.masked" && echo "ok   stdout byte-identical modulo wall" || { echo "FAIL stdout differs"; FAILED=1; }
  for f in "$EV"/det1/*.json; do
    cmp "$f" "$EV/det2/$(basename "$f")" || { echo "FAIL $f differs"; FAILED=1; }
  done
  [ "$FAILED" = 0 ] && echo "ok   min.json files byte-identical"
  ;;
all) for s in launch doctor run-toy run-fixed replay shrink determinism; do
       "$0" $s || FAILED=1; done ;;
cleanup)
  # Removes the venv and nothing else. Evidence under runs/verify/ is kept on purpose.
  rm -rf "$VENV"; echo "removed $VENV; evidence kept in runs/verify/"
  ;;
*) echo "usage: $0 <launch|doctor|run-toy|run-fixed|replay|shrink|determinism|all|cleanup>"; exit 2 ;;
esac
exit "$FAILED"
