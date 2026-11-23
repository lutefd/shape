"""Verify that the README quickstart commands, guide examples, and doc links stay valid.

Run from the repository root:
    python scripts/check_docs.py
"""

from __future__ import annotations

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
        run([*pytest_cmd, "docs/guide/examples/test_async_agent.py"], expect=0)

    # 3. Local relative links across README and user guide
    check_links()
    run([sys.executable, "scripts/sync_fake_verification.py", "--check"], expect=0)
    print("docs check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
