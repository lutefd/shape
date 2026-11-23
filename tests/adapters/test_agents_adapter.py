"""Tests for the OpenAI Agents SDK adapter (`shape.agents`). Skipped when openai-agents is not
installed."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("agents")

from agents import Agent, RunContextWrapper, SQLiteSession, function_tool

import shape
import shape.agents
from shape.boundary import runner
from shape.boundary.faults import FAULTS


def _fault(name: str) -> int:
    return FAULTS.index(name) + 1


@dataclass
class _Ctx:
    db_path: Path
    keyed: bool = False
    allowed_orders: frozenset[str] | None = None

    def init_db(self) -> None:
        with sqlite3.connect(self.db_path, timeout=30) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS refunds ("
                "id INTEGER PRIMARY KEY, order_id TEXT, amount INTEGER, idempotency_key TEXT UNIQUE)"
            )

    def has_refund(self, order_id: str) -> bool:
        with sqlite3.connect(self.db_path, timeout=30) as db:
            return (
                db.execute("SELECT 1 FROM refunds WHERE order_id = ? LIMIT 1", (order_id,)).fetchone()
                is not None
            )


@function_tool
def _lookup_order(ctx: RunContextWrapper[_Ctx], order_id: str) -> str:
    """Look up an order (sync tool)."""
    return json.dumps({"order_id": order_id, "amount": 4200})


async def _issue_refund(ctx: RunContextWrapper[_Ctx], order_id: str, amount: int) -> str:
    """Issue a refund (async tool)."""
    if ctx.context.allowed_orders is not None and order_id not in ctx.context.allowed_orders:
        raise PermissionError(f"order {order_id} not allowed")
    key = f"t1:refund:{order_id}" if ctx.context.keyed else None
    with sqlite3.connect(ctx.context.db_path, timeout=30) as db:
        if key is not None:
            row = db.execute("SELECT id FROM refunds WHERE idempotency_key = ?", (key,)).fetchone()
            if row:
                return f"re_{row[0]}"
        cur = db.execute(
            "INSERT INTO refunds (order_id, amount, idempotency_key) VALUES (?, ?, ?)",
            (order_id, amount, key),
        )
        return f"re_{cur.lastrowid}"


def _build_agent(
    *,
    keyed: bool = False,
    approval: bool = False,
    alternatives: dict | None = None,
    plan: list | None = None,
):
    tools = shape.agents.effects(
        [_lookup_order, function_tool(_issue_refund, name_override="issue_refund", needs_approval=approval)],
        ops={
            "issue_refund": {
                "op": "payments.refund",
                "scope": lambda order_id, **_: f"order:{order_id}",
                "key": lambda order_id, **_: f"t1:refund:{order_id}" if keyed else None,
            }
        },
    )
    default_plan = [
        ("_lookup_order", {"order_id": "17"}),
        ("issue_refund", {"order_id": "17", "amount": 4200}),
    ]
    model = shape.agents.ScriptedModel(
        plan if plan is not None else default_plan,
        final="Order 17 is refunded.",
        alternatives=alternatives,
    )
    return Agent(name="Support", instructions="Refund orders.", model=model, tools=tools)


def test_effects_wraps_sync_and_async_function_tools(tmp_path):
    @function_tool
    def sync_t(order_id: str) -> str:
        """Sync tool."""
        return f"s:{order_id}"

    @function_tool
    async def async_t(order_id: str) -> str:
        """Async tool."""
        await asyncio.sleep(0)
        return f"a:{order_id}"

    @function_tool
    def read_t(order_id: str) -> str:
        """Read-only tool."""
        return f"r:{order_id}"

    wrapped = shape.agents.effects(
        [sync_t, async_t, read_t],
        ops={
            "sync_t": "t.sync",
            "async_t": shape.agents.Effect("t.async", scope=lambda order_id, **_: order_id),
        },
    )
    assert wrapped[2] is read_t

    with pytest.raises(ValueError, match="unknown tool"):
        shape.agents.effects([sync_t], ops={"missing": "t.missing"})

    model = shape.agents.ScriptedModel(
        [("sync_t", {"order_id": "1"}), ("async_t", {"order_id": "2"})], final="ok"
    )
    agent = Agent(name="T", instructions="test", model=model, tools=wrapped)

    @shape.scenario(
        invariants=[shape.exactly_once("t.sync"), shape.exactly_once("t.async")],
        max_restarts=0,
    )
    def run_both(run: shape.Run) -> None:
        res = shape.agents.drive(agent, "go", run)
        assert res.final_output == "ok"

    out = runner.execute(run_both, {"explicit": {}}, tmp_path / "ok")
    assert out.ok, out.violations
    assert [e["op"] for e in out.entries if e["kind"] == "effect"] == ["t.sync", "t.async"]


def test_repeat_perturbation_and_fault_retry_and_crash_resume(tmp_path):
    @shape.scenario(invariants=[shape.at_most_once("payments.refund")], max_restarts=2)
    def unkeyed(run: shape.Run) -> None:
        ctx = _Ctx(run.dir / "p.db", keyed=False)
        ctx.init_db()
        agent = _build_agent(keyed=False)
        shape.agents.drive(agent, "Please refund order 17.", run, context=ctx)

    # Pure model perturbation (0 faults) shrinks to repeat choice
    with pytest.raises(AssertionError) as info:
        shape.check(unkeyed, seeds=120, runs_dir=tmp_path)
    text = str(info.value)
    assert "at_most_once(payments.refund)" in text
    assert "NOT MINIMIZED" not in text

    # Explicit repeat choice trace reads as specified
    rep = runner.execute(unkeyed, {"explicit": {"model:call 2: issue_refund#0.0": 1}}, tmp_path / "rep")
    assert rep.violated("at_most_once(payments.refund)")
    lines, _ = runner.story(rep.entries)
    assert any("model chose 'repeat' at 'call 2: issue_refund'" in line for line in lines)

    # Explicit connection_reset_after_commit -> SDK hands error to model -> nominal retry -> 2 effects
    conn = runner.execute(
        unkeyed,
        {"explicit": {"fault:payments.refund#0.0": _fault("connection_reset_after_commit")}},
        tmp_path / "conn",
    )
    assert conn.violated("at_most_once(payments.refund)")

    # Explicit crash_after -> restarted incarnation redelivers user turn into SQLiteSession -> 2 effects
    crash = runner.execute(
        unkeyed,
        {"explicit": {"fault:payments.refund#0.0": _fault("crash_after")}},
        tmp_path / "crash",
    )
    assert crash.violated("at_most_once(payments.refund)")
    eff_incs = [e["inc"] for e in crash.entries if e["kind"] == "effect"]
    assert eff_incs == [0, 1]


def test_substitute_perturbation_fails_unguarded_and_passes_with_allowlist(tmp_path):
    @shape.scenario(
        invariants=[shape.at_most_once("payments.refund"), shape.approved_before("payments.refund")],
        max_restarts=3,
    )
    def unguarded(run: shape.Run) -> None:
        shape.approval("order:17", {"order_id": "17", "amount": 4200})
        ctx = _Ctx(run.dir / "p.db", keyed=True)
        ctx.init_db()
        agent = _build_agent(keyed=True, alternatives={"order_id": ["18"]})
        shape.agents.drive(agent, "Refund order 17.", run, context=ctx)

    with pytest.raises(AssertionError) as info:
        shape.check(unguarded, seeds=150, runs_dir=tmp_path)
    text = str(info.value)
    assert "approved_before(payments.refund)" in text
    assert "substitute order_id='18'" in text
    assert "NOT MINIMIZED" not in text

    @shape.scenario(
        invariants=[shape.at_most_once("payments.refund"), shape.approved_before("payments.refund")],
        max_restarts=3,
    )
    def guarded(run: shape.Run) -> None:
        shape.approval("order:17", {"order_id": "17", "amount": 4200})
        ctx = _Ctx(run.dir / "p.db", keyed=True, allowed_orders=frozenset({"17"}))
        ctx.init_db()
        agent = _build_agent(keyed=True, alternatives={"order_id": ["18"]})
        shape.agents.drive(agent, "Refund order 17.", run, context=ctx)

    shape.check(guarded, seeds=100, runs_dir=tmp_path)


def test_drive_approval_and_custom_session_factory(tmp_path):
    @shape.scenario(
        invariants=[
            shape.at_most_once("payments.refund"),
            shape.approved_before("payments.refund"),
        ],
        max_restarts=2,
    )
    def approval_flow(run: shape.Run) -> None:
        ctx = _Ctx(run.dir / "p.db", keyed=True)
        ctx.init_db()
        agent = _build_agent(keyed=True, approval=True)

        def human(item) -> bool:
            args = json.loads(item.arguments)
            decision = shape.choose("approver", [True, False])
            if decision:
                shape.approval(f"order:{args['order_id']}", args)
            return decision

        shape.agents.drive(
            agent,
            "Please refund order 17.",
            run,
            context=ctx,
            session_factory=lambda d: SQLiteSession("custom", str(d / "custom.db")),
            human=human,
        )

    shape.check(approval_flow, seeds=60, runs_dir=tmp_path)
