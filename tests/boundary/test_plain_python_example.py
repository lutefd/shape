import json
from pathlib import Path

from shape import cli

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "plain_python" / "checks.py"


def test_the_buggy_retry_is_found_and_shrunk(tmp_path, capsys):
    code = cli.main(["test", f"{EXAMPLE}::checkout_buggy", "--seeds", "200",
                     "--runs-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "invariant=at_most_once(payments.charge)" in out
    assert "connection reset after commit" in out
    replay = next(line for line in out.splitlines() if line.startswith("  replay: "))
    min_file = Path(replay.removeprefix("  replay: "))
    assert len(json.loads(min_file.read_text())["realized"]) <= 2
    # the saved file replays to the same failure
    code = cli.main(["test", f"{EXAMPLE}::checkout_buggy", "--replay", str(min_file),
                     "--runs-dir", str(tmp_path)])
    assert code == 1
    assert "(replayed)" in capsys.readouterr().out


def test_the_fixed_retry_passes(tmp_path, capsys):
    code = cli.main(["test", f"{EXAMPLE}::checkout_fixed", "--seeds", "200", "--jobs", "2",
                     "--runs-dir", str(tmp_path)])
    assert code == 0, capsys.readouterr().out
