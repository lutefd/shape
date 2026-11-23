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
        shape.agents.drive(
            agent, "Please refund order 17.", run, context=ctx, on_restart="redeliver"
        )

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


@dataclass
class _TenantCtx:
    tenant: str
    allow: bool = True


def test_agents_adapter_passes_context_and_supports_landed_key_from_result_and_harness_errors(
    tmp_path,
):
    @function_tool
    def open_case(wrapper: RunContextWrapper[_TenantCtx], subject: str) -> str:
        """Open a support case."""
        if not wrapper.context.allow:
            return "refused: account frozen"
        return json.dumps({"case_id": f"{wrapper.context.tenant}-5170", "subject": subject})

    # 1. Context (via wrapper, ctx, and context) flows into scope, key_from_result, result, landed
    tools = shape.agents.effects(
        [open_case],
        ops={
            "open_case": {
                "op": "cases.open",
                "scope": lambda wrapper, subject, **_: f"{wrapper.context.tenant}:{subject}",
                "landed": lambda res, ctx, **_: (
                    ctx.context.tenant == "acme" and not res.startswith("refused:")
                ),
                "key_from_result": lambda res, context, **_: (
                    json.loads(res)["case_id"] if context.context.allow else None
                ),
                "result": lambda res, ctx, **_: f"{ctx.context.tenant}:{json.loads(res)['case_id']}",
            }
        },
    )
    model = shape.agents.ScriptedModel(
        [("open_case", {"subject": "billing"}), ("open_case", {"subject": "billing"})],
        final="done",
    )
    agent = Agent(name="Support", instructions="Open cases.", model=model, tools=tools)

    @shape.scenario(invariants=[shape.at_most_once("cases.open")], max_restarts=0)
    def deduped_with_ctx(run: shape.Run) -> None:
        shape.agents.drive(agent, "open", run, context=_TenantCtx(tenant="acme", allow=True))

    out = runner.execute(deduped_with_ctx, {"explicit": {}}, tmp_path / "ctx")
    assert out.ok, out.violations
    effects = [e for e in out.entries if e["kind"] == "effect"]
    assert len(effects) == 2
    assert effects[0]["scope"] == "acme:billing"
    assert effects[0]["key"] == "acme-5170"
    assert effects[0]["result"] == "acme:acme-5170"
    # Framework context must not pollute rendered call or args
    assert "wrapper" not in effects[0]["call"] and "ctx" not in effects[0]["args"]

    # 2. Refusal string with landed= logs no_effect, not effect
    @shape.scenario(invariants=[shape.at_most_once("cases.open")], max_restarts=0)
    def refused_with_ctx(run: shape.Run) -> None:
        shape.agents.drive(agent, "open", run, context=_TenantCtx(tenant="acme", allow=False))

    out_ref = runner.execute(refused_with_ctx, {"explicit": {}}, tmp_path / "ref")
    assert [e for e in out_ref.entries if e["kind"] == "effect"] == []
    assert len([e for e in out_ref.entries if e["kind"] == "no_effect"]) == 2
    trace, _ = runner.story(out_ref.entries)
    assert any("returned without effect" in line for line in trace)

    # 3. Harness error in scope= is NEVER swallowed by Agents SDK into a tool error string
    bad_tools = shape.agents.effects(
        [open_case],
        ops={"open_case": {"op": "cases.open", "scope": lambda missing_arg: missing_arg}},
    )
    bad_agent = Agent(
        name="Support",
        instructions="Open cases.",
        model=shape.agents.ScriptedModel([("open_case", {"subject": "billing"})], final="done"),
        tools=bad_tools,
    )

    @shape.scenario(invariants=[shape.at_most_once("cases.open")], max_restarts=2)
    def bad_scope(run: shape.Run) -> None:
        shape.agents.drive(bad_agent, "open", run, context=_TenantCtx(tenant="acme"))

    out_bad = runner.execute(bad_scope, {"explicit": {}}, tmp_path / "bad_scope")
    assert not out_bad.ok
    assert out_bad.violations[0].invariant == "harness_error"
    assert "your scope for cases.open raised TypeError:" in out_bad.violations[0].message
    assert "test_agents_adapter.py:" in out_bad.violations[0].message


def test_agents_adapter_auto_detects_framework_error_string_as_not_landed(tmp_path):
    from agents import FunctionTool

    async def converted_error(_ctx, _raw: str) -> str:
        return "An error occurred while running the tool. Please try again."

    raw_tool = FunctionTool(
        name="flaky_tool",
        description="Returns converted framework error.",
        params_json_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        on_invoke_tool=converted_error,
    )
    wrapped = shape.agents.effects([raw_tool], ops={"flaky_tool": "t.flaky"})
    model = shape.agents.ScriptedModel([("flaky_tool", {"x": "1"})], final="done")
    agent = Agent(name="A", instructions="i", model=model, tools=wrapped)

    @shape.scenario(invariants=[shape.at_most_once("t.flaky")], max_restarts=0)
    def run_converted(run: shape.Run) -> None:
        shape.agents.drive(agent, "go", run)

    out = runner.execute(run_converted, {"explicit": {}}, tmp_path / "conv")
    assert [e for e in out.entries if e["kind"] == "effect"] == []
    assert len([e for e in out.entries if e["kind"] == "no_effect"]) >= 1


def test_scenario_recovery_redelivers_agents_sdk_input(tmp_path):
    @shape.scenario(recovery=shape.recovery.CeleryAcksLate(reject_on_worker_lost=True), max_restarts=1)
    def scenario(run):
        ctx = _Ctx(run.dir / "payments.db")
        ctx.init_db()
        shape.agents.drive(_build_agent(), "Please refund order 17.", run, context=ctx)

    spec = {"explicit": {"fault:payments.refund#0.0": _fault("crash_after")}}
    outcome = runner.execute(scenario, spec, tmp_path)
    assert outcome.stats["effects"] == {"payments.refund": 2}
    assert any("harness: Celery redelivered" in line for line in runner.story(outcome.entries)[0])
