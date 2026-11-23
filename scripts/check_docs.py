"""Verify that the README quickstart commands, guide examples, and doc links stay valid.

Run from the repository root:
    python scripts/check_docs.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def run(cmd: list[str], *, expect: int, must_contain: list[str] = ()) -> str:
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    out = proc.stdout + proc.stderr
    if proc.returncode != expect:
        raise AssertionError(
            f"command {' '.join(cmd)!r} exited {proc.returncode}, expected {expect}:\n{out}"
        )
    for needle in must_contain:
        if needle not in out:
            raise AssertionError(
                f"command {' '.join(cmd)!r} output missing {needle!r}:\n{out}"
            )
    return out


def check_links() -> None:
    doc_files = [
        ROOT / "README.md",
        ROOT / "docs" / "boundary.md",
        ROOT / "docs" / "adapters.md",
        *sorted((ROOT / "docs" / "guide").glob("*.md")),
    ]
    broken: list[str] = []
    for doc in doc_files:
        text = doc.read_text()
        for target in LINK_RE.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            resolved = (doc.parent / path_part).resolve()
            if not resolved.exists():
                broken.append(f"{doc.relative_to(ROOT)} -> {target}")
    if broken:
        raise AssertionError("broken documentation links:\n  " + "\n  ".join(broken))


def main() -> int:
    shape_cmd = [sys.executable, "-m", "shape.cli"]
    pytest_cmd = [sys.executable, "-m", "pytest", "-q"]

    with tempfile.TemporaryDirectory(prefix="shape-docs-check-") as tmp:
        runs = Path(tmp)

        # 1. README 5-minute quickstart commands
        run(
            [*shape_cmd, "test", "examples/plain_python/checks.py::checkout_buggy",
             "--seeds", "200", "--runs-dir", str(runs)],
            expect=1,
            must_contain=[
                "1 defect across 2 failing seeds",
                "reply lost: 2 seeds",
                "invariant=at_most_once(payments.charge)",
                "connection reset after commit",
                "seed-52.min.json",
            ],
        )
        run(
            [*shape_cmd, "test", "examples/plain_python/checks.py::checkout_fixed",
             "--seeds", "200", "--runs-dir", str(runs)],
            expect=0,
            must_contain=["PASS"],
        )
        run(
            [*shape_cmd, "test", "examples/plain_python/checks.py::checkout_buggy",
             "--replay", str(runs / "checkout_buggy" / "seed-52.min.json"),
             "--runs-dir", str(runs)],
            expect=1,
            must_contain=["(replayed)", "invariant=at_most_once(payments.charge)"],
        )

        # 2. Guide runnable examples (async agent + built-in invariants + pytest example)
        run(
            [*shape_cmd, "test", "docs/guide/examples/async_agent.py::async_per_attempt_key",
             "--seeds", "200", "--runs-dir", str(runs)],
            expect=1,
            must_contain=["invariant=at_most_once(payments.charge)"],
        )
        run(
            [*shape_cmd, "test", "docs/guide/examples/async_agent.py::async_stable_key",
             "--seeds", "200", "--runs-dir", str(runs)],
            expect=0,
            must_contain=["PASS"],
        )
        run(
            [*shape_cmd, "test", "docs/guide/examples/invariants.py",
             "--seeds", "50", "--runs-dir", str(runs)],
            expect=1,
            must_contain=[
                "invariant=at_most_once(payments.charge)",
                "invariant=exactly_once(payments.charge)",
                "invariant=approved_before(transfers.send)",
                "invariant=requires(email.send, before=payments.charge)",
                "invariant=within_scopes(payments.refund)",
                "invariant=max_calls(payments.charge, 3)",
                "invariant=sum_le(payments.refund.amount, 500)",
                "invariant=claims_true",
                "invariant=refund_matches_request",
            ],
        )
        run(
            [*shape_cmd, "test", "docs/guide/examples/async_agent.py::async_per_attempt_key",
             "--start", "13", "--seeds", "1", "--runs-dir", str(runs)],
            expect=1,
            must_contain=["harness: cron re-ticked the job (recovery='cron-retick')"],
        )
        run([*pytest_cmd, "docs/guide/examples/test_async_agent.py"], expect=0)

        # 3. World fixtures guide: setup, per-seed SQLite, virtual clock, presets, by-design loss
        world = "docs/guide/examples/world_fixtures.py"
        run(
            [*shape_cmd, "test", f"{world}::lease_unkeyed", "--start", "39", "--seeds", "1",
             "--runs-dir", str(runs)],
            expect=1,
            must_contain=[
                "setup (no faults)",
                "enqueue_job(job_id='j1')",
                "time passed: 31s (clock 2025-01-01T00:00:31Z)",
                ("harness: another worker reclaimed the job after its lease expired "
                 "(recovery='lease-takeover')"),
                "agent.run happened 2 times for scope 'job:j1'",
            ],
        )
        run([*shape_cmd, "test", f"{world}::lease_keyed", "--seeds", "40", "--runs-dir", str(runs)],
            expect=0, must_contain=["PASS"])
        run(
            [*shape_cmd, "test", f"{world}::digest_current_hour", "--start", "2", "--seeds", "1",
             "--runs-dir", str(runs)],
            expect=1,
            must_contain=[
                "no digest was sent for hour 0",
                "time passed: 3600s (clock 2025-01-01T01:00:00Z)",
                ("harness: APScheduler reloaded the job store; the crashed tick was not rerun "
                 "(recovery='apscheduler-reload')"),
                "harness: tick scheduled for 2025-01-01T01:00:00Z",
            ],
        )
        run([*shape_cmd, "test", f"{world}::digest_catches_up", "--seeds", "100",
             "--runs-dir", str(runs)], expect=0, must_contain=["PASS"])
        run(
            [*shape_cmd, "test", f"{world}::sms_unkeyed", "--seeds", "100", "--runs-dir", str(runs)],
            expect=1,
            must_contain=[
                ("by design (declared at-most-once, a lost effect is not a defect; "
                 "a duplicate still fails):"),
                "sms.send: lost in 14 of 100 seeds",
                "sms.send happened 2 times for scope 'user:u1'",
            ],
        )
        run(
            [*shape_cmd, "test", f"{world}::sms_keyed", "--seeds", "100", "--runs-dir", str(runs)],
            expect=0,
            must_contain=["sms.send: lost in 14 of 100 seeds", "PASS"],
        )
        if os.environ.get("SHAPE_PG_DSN"):
            pg = "docs/guide/examples/postgres_queue.py"
            run([sys.executable, pg], expect=0, must_contain=["template database"])
            run([*shape_cmd, "test", f"{pg}::pg_lease_unkeyed", "--start", "39", "--seeds", "1",
                 "--runs-dir", str(runs)], expect=1,
                must_contain=["time passed: 31s", "agent.run happened 2 times for scope 'job:j1'"])
            run([*shape_cmd, "test", f"{pg}::pg_lease_keyed", "--seeds", "100", "--jobs", "4",
                 "--runs-dir", str(runs)], expect=0, must_contain=["PASS"])

    # 3. Local relative links across README and user guide
    check_links()
    run([sys.executable, "scripts/sync_fake_verification.py", "--check"], expect=0)
    print("docs check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
