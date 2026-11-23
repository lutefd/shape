"""Tests for the LangGraph adapter (`shape.langgraph`). Skipped when langgraph is not installed."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Annotated, Literal, TypedDict

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langgraph.checkpoint.sqlite")

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool, tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import InjectedState, ToolNode, ToolRuntime, tools_condition
from langgraph.types import Command, interrupt

import shape
import shape.langgraph
from shape.boundary import runner
from shape.boundary.faults import FAULTS


def _fault(name: str) -> int:
    return FAULTS.index(name) + 1


def _build_refund_graph(
    db_path: Path,
    *,
    keyed: bool = False,
    alternatives: dict | None = None,
    allowed_orders: frozenset[str] | None = None,
    plan: list | None = None,
):
    with sqlite3.connect(db_path, timeout=30) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS refunds ("
            "id INTEGER PRIMARY KEY, order_id TEXT, amount INTEGER, idempotency_key TEXT UNIQUE)"
        )

    @tool
    def lookup_order(order_id: str) -> str:
        """Look up an order."""
        return json.dumps({"order_id": order_id, "amount": 4200})

    @tool
    def issue_refund(order_id: str, amount: int) -> str:
        """Issue a refund."""
        if allowed_orders is not None and order_id not in allowed_orders:
            raise PermissionError(f"order {order_id} not allowed")
        key = f"t1:refund:{order_id}" if keyed else None
        with sqlite3.connect(db_path, timeout=30) as db:
            if key is not None:
                row = db.execute(
                    "SELECT id FROM refunds WHERE idempotency_key = ?", (key,)
                ).fetchone()
                if row:
                    return f"re_{row[0]}"
            cur = db.execute(
                "INSERT INTO refunds (order_id, amount, idempotency_key) VALUES (?, ?, ?)",
                (order_id, amount, key),
            )
            return f"re_{cur.lastrowid}"

    wrapped = shape.langgraph.effects(
        [lookup_order, issue_refund],
        ops={
            "issue_refund": {
                "op": "payments.refund",
                "scope": lambda order_id, **_: f"order:{order_id}",
                "key": lambda order_id, **_: f"t1:refund:{order_id}" if keyed else None,
            }
        },
    )
    default_plan = [
        ("lookup_order", {"order_id": "17"}),
        ("issue_refund", {"order_id": "17", "amount": 4200}),
    ]
    model = shape.langgraph.ScriptedChatModel(
        plan if plan is not None else default_plan,
        final="Order 17 is refunded.",
        alternatives=alternatives,
    ).bind_tools(wrapped)

    builder = StateGraph(MessagesState)
    builder.add_node("agent", lambda s: {"messages": [model.invoke(s["messages"])]})
    builder.add_node("tools", ToolNode(wrapped, handle_tool_errors=True))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    return builder


def test_effects_wraps_sync_async_structured_and_base_tools(tmp_path):
    @tool
    def sync_op(order_id: str) -> str:
        """Sync tool."""
        return f"sync:{order_id}"

    @tool
    async def async_op(order_id: str) -> str:
        """Async tool."""
        await asyncio.sleep(0)
        return f"async:{order_id}"

    @tool
    def read_only(order_id: str) -> str:
        """Read-only tool."""
        return f"read:{order_id}"

    class CustomTool(BaseTool):
        name: str = "custom_op"
        description: str = "Custom BaseTool subclass."

        def _run(self, order_id: str) -> str:
            return f"custom:{order_id}"

    both = StructuredTool.from_function(
        name="dual_op",
        description="Both sync and async.",
        func=lambda order_id: f"s:{order_id}",
        coroutine=_make_async_fn(),
    )

    wrapped = shape.langgraph.effects(
        [sync_op, async_op, read_only, CustomTool(), both],
        ops={
            "sync_op": {"op": "t.sync", "scope": lambda order_id, **_: order_id},
            "async_op": shape.langgraph.Effect("t.async", scope=lambda order_id, **_: order_id),
            "custom_op": "t.custom",
            "dual_op": "t.dual",
        },
    )
    w_sync, w_async, w_read, w_custom, w_dual = wrapped
    assert w_read is read_only
    # Outside a shape run, tools work identically
    assert w_sync.invoke({"order_id": "1"}) == "sync:1"
    assert asyncio.run(w_async.ainvoke({"order_id": "2"})) == "async:2"
    assert w_custom.invoke({"order_id": "3"}) == "custom:3"
    assert w_dual.invoke({"order_id": "4"}) == "s:4"
    assert asyncio.run(w_dual.ainvoke({"order_id": "5"})) == "a:5"

    with pytest.raises(ValueError, match="unknown tool"):
        shape.langgraph.effects([sync_op], ops={"nope": "t.nope"})

    # Inside a shape run, both sync and async effects record into world log and inject faults
    @shape.scenario(
        invariants=[shape.exactly_once("t.sync"), shape.exactly_once("t.async")],
        max_restarts=0,
    )
    def run_both(run: shape.Run) -> None:
        w_sync.invoke({"order_id": "1"})
        asyncio.run(w_async.ainvoke({"order_id": "2"}))
        w_custom.invoke({"order_id": "3"})

    out = runner.execute(run_both, {"explicit": {}}, tmp_path / "nominal")
    assert out.ok, out.violations
    ops = [e["op"] for e in out.entries if e["kind"] == "effect"]
    assert ops == ["t.sync", "t.async", "t.custom"]

    out_crash = runner.execute(
        run_both, {"explicit": {"fault:t.async#0.0": _fault("crash_before")}}, tmp_path / "crash"
    )
    assert not out_crash.completed
    ops_crash = [e["op"] for e in out_crash.entries if e["kind"] == "effect"]
    assert ops_crash == ["t.sync"]


def _make_async_fn():
    async def _fn(order_id: str) -> str:
        await asyncio.sleep(0)
        return f"a:{order_id}"

    return _fn


def test_repeat_perturbation_fails_unguarded_and_passes_keyed(tmp_path):
    @shape.scenario(invariants=[shape.at_most_once("payments.refund")], max_restarts=3)
    def unguarded(run: shape.Run) -> None:
        builder = _build_refund_graph(run.dir / "p.db", keyed=False)
        with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as cp:
            graph = builder.compile(checkpointer=cp)
            shape.langgraph.drive(
                graph,
                {"messages": [HumanMessage(content="refund 17")]},
                {"configurable": {"thread_id": "t1"}},
                run,
                durability="sync",
            )

    with pytest.raises(AssertionError) as info:
        shape.check(unguarded, seeds=120, runs_dir=tmp_path)
    text = str(info.value)
    assert "at_most_once(payments.refund)" in text
    assert "NOT MINIMIZED" not in text

    # Explicit repeat choice (0 faults) renders the exact choice trace
    rep = runner.execute(
        unguarded, {"explicit": {"model:call 2: issue_refund#0.0": 1}}, tmp_path / "rep"
    )
    assert rep.violated("at_most_once(payments.refund)")
    lines, _ = runner.story(rep.entries)
    assert any("model chose 'repeat' at 'call 2: issue_refund'" in line for line in lines)

    @shape.scenario(invariants=[shape.at_most_once("payments.refund")], max_restarts=3)
    def guarded(run: shape.Run) -> None:
        builder = _build_refund_graph(run.dir / "p.db", keyed=True)
        with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as cp:
            graph = builder.compile(checkpointer=cp)
            shape.langgraph.drive(
                graph,
                {"messages": [HumanMessage(content="refund 17")]},
                {"configurable": {"thread_id": "t1"}},
                run,
                durability="sync",
            )

    shape.check(guarded, seeds=100, runs_dir=tmp_path)


def test_substitute_perturbation_fails_unguarded_and_passes_with_allowlist(tmp_path):
    @shape.scenario(
        invariants=[shape.at_most_once("payments.refund"), shape.approved_before("payments.refund")],
        max_restarts=3,
    )
    def unguarded(run: shape.Run) -> None:
        shape.approval("order:17", {"order_id": "17", "amount": 4200})
        builder = _build_refund_graph(
            run.dir / "p.db", keyed=True, alternatives={"order_id": ["18"]}
        )
        with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as cp:
            graph = builder.compile(checkpointer=cp)
            shape.langgraph.drive(
                graph,
                {"messages": [HumanMessage(content="refund 17")]},
                {"configurable": {"thread_id": "t1"}},
                run,
                durability="sync",
            )

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
        builder = _build_refund_graph(
            run.dir / "p.db",
            keyed=True,
            alternatives={"order_id": ["18"]},
            allowed_orders=frozenset({"17"}),
        )
        with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as cp:
            graph = builder.compile(checkpointer=cp)
            shape.langgraph.drive(
                graph,
                {"messages": [HumanMessage(content="refund 17")]},
                {"configurable": {"thread_id": "t1"}},
                run,
                durability="sync",
            )

    shape.check(guarded, seeds=100, runs_dir=tmp_path)


def test_drop_swap_and_error_retry_perturbations(tmp_path):
    @shape.scenario(invariants=[shape.exactly_once("payments.refund")], max_restarts=0)
    def drop_case(run: shape.Run) -> None:
        builder = _build_refund_graph(run.dir / "p.db", keyed=True)
        with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as cp:
            graph = builder.compile(checkpointer=cp)
            shape.langgraph.drive(
                graph,
                {"messages": [HumanMessage(content="refund 17")]},
                {"configurable": {"thread_id": "t1"}},
                run,
            )

    # Drop call 2 -> exactly_once violated
    out = runner.execute(drop_case, {"explicit": {"model:call 2: issue_refund#0.0": 2}}, tmp_path / "d")
    assert out.violated("exactly_once(payments.refund)")

    # Error retry_0 vs retry_1 vs retry_2 on timeout fault
    out_r0 = runner.execute(
        drop_case,
        {
            "explicit": {
                "fault:payments.refund#0.0": _fault("timeout"),
                "model:error at call 2: issue_refund#0.0": 1,  # retry_0
            }
        },
        tmp_path / "r0",
    )
    assert out_r0.violated("exactly_once(payments.refund)")

    out_r1 = runner.execute(
        drop_case,
        {"explicit": {"fault:payments.refund#0.0": _fault("timeout")}},
        tmp_path / "r1",
    )
    assert out_r1.ok, out_r1.violations

    out_r2 = runner.execute(
        drop_case,
        {
            "explicit": {
                "fault:payments.refund#0.0": _fault("timeout"),
                "fault:payments.refund#0.1": _fault("timeout"),
                "model:error at call 2: issue_refund#0.0": 2,  # retry_2
            }
        },
        tmp_path / "r2",
    )
    assert out_r2.ok, out_r2.violations


def test_drive_interrupt_with_shape_choose_human_callback(tmp_path):
    class GateState(TypedDict):
        amount: int
        decision: str

    @shape.effect("wire.send", scope=lambda **_: "w1", key=lambda **_: "k1", irreversible=True)
    def send_wire(amount: int) -> int:
        return amount

    @shape.scenario(invariants=[shape.approved_before("wire.send")], max_restarts=2)
    def gate_with_choose(run: shape.Run) -> None:
        with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as cp:
            def approve_node(state: GateState) -> Command[Literal["send", "cancel"]]:
                reply = interrupt({"amount": state["amount"]})
                if reply["action"] == "reject":
                    return Command(goto="cancel", update={"decision": "rejected"})
                amt = reply.get("amount", state["amount"])
                shape.approval("w1", {"amount": amt})
                return Command(goto="send", update={"amount": amt, "decision": reply["action"]})

            def send_node(state: GateState) -> dict:
                send_wire(state["amount"])
                return {"decision": "sent"}

            b = StateGraph(GateState)
            b.add_node("approve", approve_node)
            b.add_node("send", send_node)
            b.add_node("cancel", lambda _s: {"decision": "cancelled"})
            b.add_edge(START, "approve")
            b.add_edge("send", END)
            b.add_edge("cancel", END)
            g = b.compile(checkpointer=cp)

            def human(payload):
                return shape.choose(
                    "human_review",
                    [
                        {"action": "approve"},
                        {"action": "reject"},
                        {"action": "edit", "amount": payload["amount"] // 2},
                    ],
                )

            shape.langgraph.drive(
                g,
                {"amount": 1000, "decision": "pending"},
                {"configurable": {"thread_id": "w1"}},
                run,
                human=human,
                durability="sync",
            )

    shape.check(gate_with_choose, seeds=40, runs_dir=tmp_path)

    # Missing human callback raises a clear error
    @shape.scenario(max_restarts=0)
    def missing_human(run: shape.Run) -> None:
        with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as cp:
            b = StateGraph(GateState)
            b.add_node("approve", lambda s: {"decision": str(interrupt(s))})
            b.add_edge(START, "approve")
            b.add_edge("approve", END)
            shape.langgraph.drive(
                b.compile(checkpointer=cp),
                {"amount": 1, "decision": "pending"},
                {"configurable": {"thread_id": "w2"}},
                run,
            )

    out = runner.execute(missing_human, {"explicit": {}}, tmp_path / "missing")
    assert not out.completed
    assert any("no human=" in e.get("text", "") for e in out.entries if e["kind"] == "error")


def test_langgraph_adapter_passes_config_injected_state_runtime_and_handles_tool_errors(tmp_path):
    @tool
    def plain_with_cfg(order_id: str) -> str:
        """Tool that does NOT declare config, yet scope/key read config."""
        return json.dumps({"case_id": f"CMP-{order_id}"})

    @tool
    def stateful_tool(
        order_id: str,
        state: Annotated[dict, InjectedState],
        config: RunnableConfig,
        runtime: ToolRuntime,
    ) -> str:
        """Tool declaring InjectedState, RunnableConfig, and ToolRuntime."""
        assert isinstance(state, dict) and "messages" in state
        assert config["configurable"]["thread_id"] == "th-42"
        assert runtime.config["configurable"]["thread_id"] == "th-42"
        return f"ok:{order_id}"

    @tool
    def returns_error_message(order_id: str) -> ToolMessage:
        """Returns a LangChain ToolMessage(status='error') directly."""
        return ToolMessage(
            content=f"cannot process {order_id}",
            tool_call_id="call_3",
            name="returns_error_message",
            status="error",
        )

    wrapped = shape.langgraph.effects(
        [plain_with_cfg, stateful_tool, returns_error_message],
        ops={
            "plain_with_cfg": {
                "op": "cases.open",
                "scope": lambda config, order_id, **_: (
                    f"{config['configurable']['thread_id']}:{order_id}"
                ),
                "key_from_result": lambda res, config, **_: (
                    f"{config['configurable']['thread_id']}:{json.loads(res)['case_id']}"
                ),
            },
            "stateful_tool": {
                "op": "orders.update",
                "scope": lambda state, runtime, order_id, **_: (
                    f"{runtime.config['configurable']['thread_id']}:{len(state['messages'])}:{order_id}"
                ),
            },
            "returns_error_message": {
                "op": "orders.fail_msg",
                "scope": lambda order_id, **_: f"order:{order_id}",
            },
        },
    )
    model = shape.langgraph.ScriptedChatModel(
        [
            ("plain_with_cfg", {"order_id": "17"}),
            ("plain_with_cfg", {"order_id": "17"}),
            ("stateful_tool", {"order_id": "17"}),
            ("returns_error_message", {"order_id": "17"}),
        ],
        final="done",
    ).bind_tools(wrapped)

    @shape.scenario(
        invariants=[
            shape.at_most_once("cases.open"),
            shape.exactly_once("orders.update"),
            shape.at_most_once("orders.fail_msg"),
        ],
        max_restarts=0,
    )
    def run_all(run: shape.Run) -> None:
        b = StateGraph(MessagesState)
        b.add_node("agent", lambda s: {"messages": [model.invoke(s["messages"])]})
        b.add_node("tools", ToolNode(wrapped, handle_tool_errors=True))
        b.add_edge(START, "agent")
        b.add_conditional_edges("agent", tools_condition)
        b.add_edge("tools", "agent")
        shape.langgraph.drive(
            b.compile(),
            {"messages": [HumanMessage(content="go")]},
            {"configurable": {"thread_id": "th-42"}},
            run,
        )

    out = runner.execute(run_all, {"explicit": {}}, tmp_path / "ctx")
    assert out.ok, out.violations
    open_effects = [e for e in out.entries if e["kind"] == "effect" and e["op"] == "cases.open"]
    assert len(open_effects) == 2
    assert open_effects[0]["scope"] == "th-42:17"
    assert open_effects[0]["key"] == "th-42:CMP-17"
    upd_effect = next(
        e for e in out.entries if e["kind"] == "effect" and e["op"] == "orders.update"
    )
    assert upd_effect["scope"].startswith("th-42:")
    # InjectedState / ToolRuntime / RunnableConfig must be hidden from call and args
    assert "state" not in upd_effect["args"] and "runtime" not in upd_effect["args"]
    assert "config" not in upd_effect["args"]
    # ToolMessage(status="error") auto-detected as not landed without user writing landed=
    assert [e for e in out.entries if e["kind"] == "effect" and e["op"] == "orders.fail_msg"] == []
    assert len(
        [e for e in out.entries if e["kind"] == "no_effect" and e["op"] == "orders.fail_msg"]
    ) >= 1

    # Harness error in scope= must NOT be swallowed by ToolNode(handle_tool_errors=True)
    bad_wrapped = shape.langgraph.effects(
        [plain_with_cfg],
        ops={"plain_with_cfg": {"op": "cases.open", "scope": lambda nope: nope}},
    )
    bad_model = shape.langgraph.ScriptedChatModel(
        [("plain_with_cfg", {"order_id": "17"})], final="done"
    ).bind_tools(bad_wrapped)

    @shape.scenario(invariants=[shape.at_most_once("cases.open")], max_restarts=2)
    def bad_scope(run: shape.Run) -> None:
        b = StateGraph(MessagesState)
        b.add_node("agent", lambda s: {"messages": [bad_model.invoke(s["messages"])]})
        b.add_node("tools", ToolNode(bad_wrapped, handle_tool_errors=True))
        b.add_edge(START, "agent")
        b.add_conditional_edges("agent", tools_condition)
        b.add_edge("tools", "agent")
        shape.langgraph.drive(
            b.compile(),
            {"messages": [HumanMessage(content="go")]},
            {"configurable": {"thread_id": "th-42"}},
            run,
        )

    out_bad = runner.execute(bad_scope, {"explicit": {}}, tmp_path / "bad_scope")
    assert not out_bad.ok
    assert out_bad.violations[0].invariant == "harness_error"
    assert "your scope for cases.open raised TypeError:" in out_bad.violations[0].message
    assert "test_langgraph_adapter.py:" in out_bad.violations[0].message


def test_drive_and_adrive_handle_static_interrupt_before_and_after(tmp_path):
    @tool
    def send_refund(order_id: str) -> str:
        """Issue a refund."""
        return f"refunded:{order_id}"

    def build_static_graph(cp, *, interrupt_before=None, interrupt_after=None):
        wrapped = shape.langgraph.effects(
            [send_refund],
            ops={
                "send_refund": {
                    "op": "payments.refund",
                    "scope": lambda order_id, **_: f"order:{order_id}",
                    "key": lambda order_id, **_: f"k:{order_id}",
                }
            },
        )
        model = shape.langgraph.ScriptedChatModel(
            [("send_refund", {"order_id": "17"})], final="done"
        ).bind_tools(wrapped)
        b = StateGraph(MessagesState)
        b.add_node("agent", lambda s: {"messages": [model.invoke(s["messages"])]})
        b.add_node("sensitive_tools", ToolNode(wrapped))
        b.add_edge(START, "agent")
        b.add_conditional_edges(
            "agent", tools_condition, {"tools": "sensitive_tools", END: END}
        )
        b.add_edge("sensitive_tools", "agent")
        return b.compile(
            checkpointer=cp,
            interrupt_before=interrupt_before,
            interrupt_after=interrupt_after,
        )

    # 1. Missing human= fails loudly instead of returning as finished
    @shape.scenario(invariants=[shape.at_most_once("payments.refund")], max_restarts=0)
    def missing_human_static(run: shape.Run) -> None:
        with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as cp:
            g = build_static_graph(cp, interrupt_before=["sensitive_tools"])
            shape.langgraph.drive(
                g,
                {"messages": [HumanMessage(content="refund 17")]},
                {"configurable": {"thread_id": "t1"}},
                run,
            )

    out_missing = runner.execute(missing_human_static, {"explicit": {}}, tmp_path / "missing")
    assert not out_missing.completed
    assert any(
        "graph paused before node 'sensitive_tools'; pass human=... to approve" in e.get("text", "")
        for e in out_missing.entries
        if e["kind"] == "error"
    )

    # 2. Approved via human(snapshot) -> resumes and completes (for both interrupt_before and after)
    for label, kw in [
        ("before", {"interrupt_before": ["sensitive_tools"]}),
        ("after", {"interrupt_after": ["agent"]}),
    ]:
        @shape.scenario(
            invariants=[
                shape.approved_before("payments.refund"),
                shape.exactly_once("payments.refund"),
            ],
            max_restarts=2,
        )
        def approved_static(run: shape.Run, _kw=kw) -> None:
            with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as cp:
                g = build_static_graph(cp, **_kw)

                def human(snapshot) -> bool:
                    if "sensitive_tools" in snapshot.next:
                        tc = snapshot.values["messages"][-1].tool_calls[0]
                        shape.approval(f"order:{tc['args']['order_id']}", tc["args"])
                    return True

                shape.langgraph.drive(
                    g,
                    {"messages": [HumanMessage(content="refund 17")]},
                    {"configurable": {"thread_id": "t1"}},
                    run,
                    human=human,
                    durability="sync",
                )

        out_ok = runner.execute(approved_static, {"explicit": {}}, tmp_path / f"ok_{label}")
        assert out_ok.ok, out_ok.violations
        # Crash right after sensitive_tools commits -> restarts cleanly
        out_crash = runner.execute(
            approved_static,
            {"explicit": {"fault:payments.refund#0.0": _fault("crash_after")}},
            tmp_path / f"crash_{label}",
        )
        assert out_crash.ok, out_crash.violations

    # 3. Rejected via human=lambda snap: False -> denies tool call, effect never lands
    @shape.scenario(
        invariants=[shape.approved_before("payments.refund")],
        max_restarts=0,
        expect_effects=False,
    )
    def rejected_static(run: shape.Run) -> None:
        with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as cp:
            g = build_static_graph(cp, interrupt_before=["sensitive_tools"])
            res = shape.langgraph.drive(
                g,
                {"messages": [HumanMessage(content="refund 17")]},
                {"configurable": {"thread_id": "t1"}},
                run,
                human=lambda _snap: False,
                durability="sync",
            )
            assert "not approved" in res["messages"][-1].content

    out_rej = runner.execute(rejected_static, {"explicit": {}}, tmp_path / "rej")
    assert out_rej.ok, out_rej.violations
    assert [e for e in out_rej.entries if e["kind"] == "effect"] == []

    # 4. Async adrive handles static interrupt_before too
    @shape.scenario(invariants=[shape.exactly_once("payments.refund")], max_restarts=0)
    def async_static(run: shape.Run) -> None:
        from langgraph.checkpoint.memory import MemorySaver

        g = build_static_graph(MemorySaver(), interrupt_before=["sensitive_tools"])
        asyncio.run(
            shape.langgraph.adrive(
                g,
                {"messages": [HumanMessage(content="refund 17")]},
                {"configurable": {"thread_id": "t1"}},
                run,
                human=lambda _snap: True,
            )
        )

    out_async = runner.execute(async_static, {"explicit": {}}, tmp_path / "async")
    assert out_async.ok, out_async.violations


@pytest.mark.parametrize(("recovery", "starts"), [
    ("langgraph-resume", 1),
    ("cron-retick", 2),
])
def test_scenario_recovery_selects_checkpoint_resume_or_fresh_input(tmp_path, recovery, starts):
    class State(TypedDict):
        job: str

    @shape.effect("jobs.send", scope=lambda: "job:17")
    def send():
        pass

    @shape.scenario(recovery=recovery, max_restarts=1)
    def scenario(run):
        def prepare(state):
            with (run.dir / "starts").open("a") as output:
                output.write(f"{state['job']}\n")
            return {}

        def execute(state):
            send()
            return {}

        builder = StateGraph(State)
        builder.add_node("prepare", prepare)
        builder.add_node("execute", execute)
        builder.add_edge(START, "prepare")
        builder.add_edge("prepare", "execute")
        builder.add_edge("execute", END)
        with SqliteSaver.from_conn_string(str(run.dir / "checkpoints.db")) as saver:
            shape.langgraph.drive(builder.compile(checkpointer=saver), {"job": "17"},
                                  {"configurable": {"thread_id": "job-17"}}, run, durability="sync")

    spec = {"explicit": {"fault:jobs.send#0.0": _fault("crash_before")}}
    outcome = runner.execute(scenario, spec, tmp_path)
    assert outcome.ok
    assert (tmp_path / "starts").read_text().splitlines() == ["17"] * starts
    assert any(f"(recovery={recovery!r})" in line for line in runner.story(outcome.entries)[0])
