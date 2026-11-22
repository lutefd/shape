import json

from shape.cli import main
from shape.shrink import ddmin, shrink
from shape.simulation import replay, run_seed


def test_ddmin_finds_the_minimal_failing_pair():
    items = list("abcdefghij")
    assert sorted(ddmin(items, lambda s: "c" in s and "h" in s)) == ["c", "h"]


def test_ddmin_returns_empty_when_nothing_is_needed_and_one_when_one_is():
    assert ddmin(list("abc"), lambda s: True) == []
    assert ddmin(list("abc"), lambda s: "b" in s) == ["b"]


def test_ddmin_is_one_minimal_for_a_non_monotone_predicate():
    def fails(s):
        return "a" in s and ("b" in s or "c" in s)

    result = ddmin(list("abcde"), fails)
    assert fails(result)
    assert all(not fails([x for x in result if x != y]) for y in result)


def test_shrink_never_drops_value_keys_and_skips_them_as_candidates():
    realized = {"a": 1, "b": 1, "id:x#0": 7, "jitter:l#0": 2}
    out = shrink(realized, lambda r: "b" in r)
    assert out == {"b": 1, "id:x#0": 7, "jitter:l#0": 2}


def test_toy_failure_shrinks_to_the_timeout_after_commit_that_matters():
    seed = next(s for s in range(200) if not run_seed("toy", s).ok)
    original = run_seed("toy", seed).realized
    noisy = {**original, "agent#5": 5, "actor:ext#7": 2, "fault:email.send#0": 1}

    def fails(r):
        return any(v.invariant == "at_most_once_effect" for v in replay("toy", r).violations)

    small = shrink(noisy, fails)
    assert len(small) <= 2 and fails(small)
    assert not fails({})
    assert any(k.startswith("fault:") and v == 3 for k, v in small.items())


def test_cli_run_finds_shrinks_and_writes_a_replay_file(tmp_path, capsys):
    code = main(["run", "--seeds", "60", "--saga", "toy", "--runs-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL seed=" in out and "invariant=at_most_once_effect" in out
    assert "timeout_after_commit" in out and "new key on retry" in out
    [path] = tmp_path.glob("*.min.json")
    data = json.loads(path.read_text())
    assert data["saga"] == "toy" and len(data["realized"]) == 1

    assert main(["replay", str(path)]) == 1
    assert "VIOLATION at_most_once_effect" in capsys.readouterr().out
    assert main(["shrink", str(path)]) == 0


def test_cli_fixed_passes(tmp_path, capsys):
    assert main(["run", "--seeds", "100", "--saga", "fixed", "--runs-dir", str(tmp_path)]) == 0
    assert "PASS" in capsys.readouterr().out
