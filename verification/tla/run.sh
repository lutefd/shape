#!/usr/bin/env bash
set -euo pipefail

# TLC 2026.11.19.180144 (tla2tools 2.0 2026-11-19, revision bc1b506).
jar_sha256=fdffef247f563ef6edbfd44958d2a82db79e7564e07cb673262d896353fa6713
jar=${TLA2TOOLS_JAR:-$HOME/.cache/tla/tla2tools.jar}
model=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
case_name=${1:-all}
case "$case_name" in
    all|fixed|toy|approval-mutant|pivot-mutant|pivot-guard|order-mutant|doubt-mutant|retry-mutant|ack-mutant|submission|compensation) ;;
    *)
        echo "Usage: $0 [all|fixed|toy|approval-mutant|pivot-mutant|pivot-guard|order-mutant|doubt-mutant|retry-mutant|ack-mutant|submission|compensation]" >&2
        exit 2
        ;;
esac

if [[ ! -f "$jar" ]]; then
    echo "Set TLA2TOOLS_JAR to the pinned tla2tools.jar described in $model/README.md" >&2
    exit 2
fi
jar=$(cd -- "$(dirname -- "$jar")" && pwd)/$(basename -- "$jar")
actual=$(sha256sum -- "$jar")
if [[ ${actual%% *} != "$jar_sha256" ]]; then
    echo "Expected TLC 2026.11.19.180144 with SHA-256 $jar_sha256" >&2
    exit 2
fi

logs=$(mktemp -d "${TMPDIR:-/tmp}/shape-tlc.XXXXXXXX")
echo "TLC logs are in $logs"
cd -- "$logs"

check() {
    local name=$1 expected=${2:-0} diagnostic=${3:-} status=0
    local log="$logs/$name.log"
    echo "=== $name ==="
    java -XX:+UseParallelGC -Xmx1g -cp "$jar" tlc2.TLC \
        -workers 1 -fp 0 -seed 1 -noGenerateSpecTE -metadir "$logs/$name-states" \
        -config "$model/$name.cfg" "$model/SagaProtocol.tla" >"$log" 2>&1 || status=$?
    rm -rf -- "$logs/$name-states"

    if [[ "$case_name" != all ]]; then
        cat -- "$log"
        return "$status"
    fi
    rg '^(TLC2 Version|Error:|Model checking completed|[[:digit:]].*states generated,|Finished in)' "$log" || true
    if [[ "$status" != "$expected" ]] || { [[ -n "$diagnostic" ]] && ! rg -Fq "$diagnostic" "$log"; }; then
        echo "Unexpected TLC result for $name (exit $status, expected $expected). See $log" >&2
        return 1
    fi
}

if [[ "$case_name" == all ]]; then
    check fixed
    check submission
    check compensation
    check pivot-guard
    check toy 12 'Invariant AtMostOnePaymentEffect is violated.'
    check approval-mutant 12 'Invariant DurableApprovalBeforeEmail is violated.'
    check pivot-mutant 12 'Invariant CleanCompensation is violated.'
    check doubt-mutant 12 'Invariant CleanCompensation is violated.'
    check retry-mutant 12 'Invariant CleanCompensation is violated.'
    check order-mutant 13 'Temporal property RecordedSagaTerminates was violated.'
    check ack-mutant 13 'Temporal property SubmissionTerminates was violated.'
    echo 'All expected TLC outcomes reproduced: the fixed runtime passes, every mutant fails.'
else
    check "$case_name"
fi
