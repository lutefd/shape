"""Render the fakes guide's verification table from packaged contract metadata."""

import argparse
from pathlib import Path

from shape.fakes import _PROVIDERS
from shape.fakes.contracts._suites import CONTRACTS

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "guide" / "fakes.md"
START = "<!-- provider-verification:start -->"
END = "<!-- provider-verification:end -->"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = GUIDE.read_text()
    before, section = text.split(START, 1)
    _, after = section.split(END, 1)
    rows = [START, "| Provider | Verification | Required environment variables |",
            "| --- | --- | --- |"]
    for name, fake in sorted(_PROVIDERS.items()):
        suite = CONTRACTS.get(name)
        settings = ", ".join(f"`{key}`" for key in suite.settings) if suite else "—"
        rows.append(f"| `{name}` | {fake.verification_status} | {settings} |")
    rows.extend((END, ""))
    new = before + "\n".join(rows).rstrip("\n") + after
    if args.check and new != text:
        raise SystemExit("Run python scripts/sync_fake_verification.py to refresh the guide")
    if not args.check and new != text:
        GUIDE.write_text(new)


if __name__ == "__main__":
    main()
