"""Tests for optional adapter imports and the framework-free plan perturbation engine."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from shape.adapters._plan import (
    Call,
    CallAction,
    Effect,
    FinalAction,
    HistoryCall,
    HistoryResult,
    HistoryUser,
    PlanRunner,
    normalize_effect,
)
from shape.boundary import api
from shape.choices import ExplicitChoices

ROOT = Path(__file__).resolve().parents[2]


def _run_python(code: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_core_import_is_dependency_free():
    proc = _run_python(
        """
import sys
import shape
assert 'langgraph' not in sys.modules
assert 'langchain_core' not in sys.modules
assert 'agents' not in sys.modules
"""
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(
    ("adapter", "blocked", "extra"),
    [
        ("langgraph", ("langgraph", "langchain_core"), "agent-shape[langgraph]"),
        ("agents", ("agents", "openai"), "agent-shape[agents]"),
    ],
)
def test_missing_framework_raises_clear_import_error(adapter, blocked, extra):
    code = f"""
import sys

class Blocker:
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in {blocked!r}:
            raise ModuleNotFoundError(f"blocked {{fullname}}")
        return None

sys.meta_path.insert(0, Blocker())
import shape
try:
    getattr(shape, {adapter!r})
except ImportError as exc:
    assert {extra!r} in str(exc), str(exc)
else:
    raise AssertionError("expected ImportError")
"""
    proc = _run_python(code)
    assert proc.returncode == 0, proc.stderr


def test_normalize_effect_accepts_str_dict_effect_and_rejects_bad_keys():
    assert normalize_effect("t", "payments.charge") == Effect(op="payments.charge")
    e = normalize_effect("t", {"op": "payments.charge", "irreversible": True})
    assert e == Effect(op="payments.charge", irreversible=True)
    assert normalize_effect("t", e) is e
    with pytest.raises(ValueError, match="unknown effect option"):
        normalize_effect("t", {"op": "x", "bogus": 1})
    with pytest.raises(ValueError, match="must include 'op'"):
        normalize_effect("t", {"irreversible": True})


def _drive_plan(runner: PlanRunner, *, fail_calls: set[int] | None = None, history=None):
    fail_calls = fail_calls or set()
    hist = list(history) if history is not None else [HistoryUser()]
    actions = []
    for _ in range(10):
        act = runner.decide(hist)
        actions.append(act)
        if isinstance(act, FinalAction):
            break
        hist.append(HistoryCall(act.call_id, act.name, act.args))
        is_err = len([a for a in actions if isinstance(a, CallAction)]) in fail_calls
        hist.append(HistoryResult(act.call_id, is_error=is_err))
    return actions


def test_plan_runner_nominal_and_all_perturbations(tmp_path):
    plan = [
        ("lookup", {"order_id": "17"}),
        Call("refund", {"order_id": "17", "amount": 4200}),
        "Refunded.",
    ]

    # Nominal (outside shape run)
    acts = _drive_plan(PlanRunner(plan, alternatives={"order_id": ["18"]}))
    assert [(a.name, a.args) for a in acts[:-1]] == [
        ("lookup", {"order_id": "17"}),
        ("refund", {"order_id": "17", "amount": 4200}),
    ]
    assert acts[-1] == FinalAction("Refunded.")

    def with_explicit(explicit: dict[str, int], *, inc: int = 0, **kw):
        ctx_dir = tmp_path / f"w-{inc}-{len(explicit)}-{hash(frozenset(explicit.items()))}"
        ctx_dir.mkdir(parents=True, exist_ok=True)
        ctx = api._Context(ctx_dir, inc, ExplicitChoices(explicit))
        api.activate(ctx)
        try:
            r = PlanRunner(plan, alternatives={"refund": {"order_id": ["18"]}})
            return _drive_plan(r, **kw)
        finally:
            api.activate(None)

    # repeat (index 1 at call 2: refund)
    acts = with_explicit({"model:call 2: refund#0.0": 1})
    assert [a.name for a in acts[:-1]] == ["lookup", "refund", "refund"]

    # drop (index 2 at call 2: refund)
    acts = with_explicit({"model:call 2: refund#0.0": 2})
    assert [a.name for a in acts[:-1]] == ["lookup"]

    # swap (index 3 at call 1: lookup)
    acts = with_explicit({"model:call 1: lookup#0.0": 3})
    assert [a.name for a in acts[:-1]] == ["refund", "lookup"]

    # substitute (index 3 at call 2: refund -> options: nominal, repeat, drop, substitute order_id='18')
    acts = with_explicit({"model:call 2: refund#0.0": 3})
    assert [(a.name, a.args) for a in acts[:-1]] == [
        ("lookup", {"order_id": "17"}),
        ("refund", {"order_id": "18", "amount": 4200}),
    ]

    # error retry_0 (index 1 -> 0 retries), retry_1 (nominal -> 1 retry), retry_2 (index 2 -> 2 retries)
    acts0 = with_explicit({"model:error at call 2: refund#0.0": 1}, fail_calls={2})
    assert [a.name for a in acts0[:-1]] == ["lookup", "refund"]
    assert acts0[-1] == FinalAction("Could not complete refund.")

    acts1 = with_explicit({}, fail_calls={2})
    assert [a.name for a in acts1[:-1]] == ["lookup", "refund", "refund"]
    assert acts1[-1] == FinalAction("Refunded.")

    acts2 = with_explicit({"model:error at call 2: refund#0.0": 2}, fail_calls={2, 3})
    assert [a.name for a in acts2[:-1]] == ["lookup", "refund", "refund", "refund"]
    assert acts2[-1] == FinalAction("Refunded.")

    # reissue after restart
    prior = [
        HistoryUser(),
        HistoryCall("c1", "lookup", {"order_id": "17"}),
        HistoryResult("c1"),
        HistoryCall("c2", "refund", {"order_id": "17", "amount": 4200}),
        HistoryResult("c2"),
        HistoryUser(),
    ]
    acts_nom = with_explicit({}, inc=1, history=prior)
    assert acts_nom == [FinalAction("Refunded.")]

    acts_reissue = with_explicit({"model:after restart: call 2: refund#1.0": 1}, inc=1, history=prior)
    assert [a.name for a in acts_reissue[:-1]] == ["refund"]
    assert acts_reissue[-1] == FinalAction("Refunded.")
