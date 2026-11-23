import json
from pathlib import Path

import shape
from shape import cli


@shape.invariant
def delivery_is_valid(world):
    delivery = world.run.dir / "delivery"
    if delivery.exists():
        return shape.Finding("duplicate delivery", op="delivery.send", scope=delivery.read_text())
    return shape.Finding("missing approval", op="delivery.send", scope="approval:abc")


@shape.scenario(invariants=[delivery_is_valid], max_restarts=0)
def different_scopes(run):
    if shape.choose("deliver", [True, False], weights=[1, 1]):
        (run.dir / "delivery").write_text(f"tenant:acme|job:job-{run.seed}")


def test_json_reports_normalized_defects_and_preserves_scope_when_shrinking(tmp_path, capsys):
    args = ["test", f"{__file__}::different_scopes", "--seeds", "20",
            "--jobs", "2", "--runs-dir", str(tmp_path)]
    assert cli.main([*args, "--json"]) == 1
    report, = json.loads(capsys.readouterr().out)["scenarios"]
    defects = {entry["scope_pattern"]: entry for entry in report["defects"]}
    assert set(defects) == {"tenant:<id>|job:<id>", "approval:<id>"}
    nominal = defects["tenant:<id>|job:<id>"]
    model = defects["approval:<id>"]
    assert len(nominal["seeds"]) > 1 and len(model["seeds"]) > 1
    assert sorted(nominal["seeds"] + model["seeds"]) == report["failing_seeds"]
    assert nominal["triggers"][0]["class"] == "nominal"
    assert model["triggers"][0]["class"] == "model"
    assert model["triggers"][0]["seeds"] == model["seeds"]
    path = Path(model["replay_path"])
    # A nominal replay still violates delivery_is_valid, but it is a different defect.
    assert json.loads(path.read_text())["realized"] == {"model:deliver#0.0": 1}
    assert cli.main([*args, "--replay", str(path)]) == 1
    assert "missing approval" in capsys.readouterr().out


def test_json_keeps_known_defects_when_the_shrink_limit_is_zero(tmp_path, capsys):
    assert cli.main(["test", f"{__file__}::different_scopes", "--seeds", "10",
                     "--runs-dir", str(tmp_path), "--json", "--max-shrinks", "0"]) == 1
    report, = json.loads(capsys.readouterr().out)["scenarios"]
    assert report["shrink_limit_skipped_seeds"] == report["failing_seeds"] == list(range(10))
    assert len(report["defects"]) == 2
    assert all(d["replay_path"] is None and d["triggers"] == [] for d in report["defects"])
