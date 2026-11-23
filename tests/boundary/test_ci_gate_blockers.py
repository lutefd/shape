"""Regression tests for the five CI-gate blockers and gaps:
1. Explicit business scopes (`strict_scopes=True`, `--strict-scopes`, scope assignment report).
2. Explicit adapter restart policies (`on_restart="none" | "redeliver" | callable`) and trace line.
3. Distinct after-commit trigger classes (`reply_lost` vs `crash_after`).
4. `SmtpFake` default invariant covering fresh sends (`smtp.send`).
5. Distinct default session per `shape.agents.drive` job (plus deliberate sharing via `session_id`).
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

import pytest
import scenarios as sc

import shape
from shape import cli
from shape.boundary import runner
from shape.boundary.failure import fault_class
from shape.boundary.faults import FAULTS
from shape.fakes import SendGridFake, SmtpFake


def _fault(name: str) -> int:
    return FAULTS.index(name) + 1


# ------------------------------------------------------------------ 1. Explicit business scopes


def test_strict_scopes_refuses_sendgrid_default_and_passes_with_explicit_scope(tmp_path):
    requests = pytest.importorskip("requests")

    def _send(job_id: str) -> None:
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            json={
                "personalizations": [{"to": [{"email": "alice@example.com"}]}],
                "from": {"email": "noreply@acme.test"},
                "subject": "Daily report",
                "content": [{"type": "text/plain", "value": f"Job {job_id}"}],
                "custom_args": {"job_id": job_id},
            },
        )
        assert resp.status_code == 202

    # Default SendGridFake without scope= under strict_scopes=True must refuse default rule
    @shape.scenario(net=[SendGridFake()], max_restarts=0, strict_scopes=True)
    def two_jobs_default_strict(run: shape.Run) -> None:
        _send("1")
        _send("2")

    out_strict = runner.execute(two_jobs_default_strict, {"explicit": {}}, tmp_path / "strict")
    assert out_strict.violated("at_most_once(sendgrid.mail.send)")
    msg = out_strict.violations[0].message
    assert "scope=" in msg
    assert "to|subject" in msg

    # Explicit business scope distinguishes the two jobs and passes strict_scopes=True
    @shape.scenario(
        net=[SendGridFake(scope=lambda req, params: f"job:{params['custom_args']['job_id']}")],
        max_restarts=0,
        strict_scopes=True,
    )
    def two_jobs_explicit_strict(run: shape.Run) -> None:
        _send("1")
        _send("2")

    out_ok = runner.execute(two_jobs_explicit_strict, {"explicit": {}}, tmp_path / "ok")
    assert out_ok.ok, out_ok.violations
    assert out_ok.stats["effects"] == {"sendgrid.mail.send": 2}


def test_strict_scopes_refuses_effect_default_and_cli_prints_scope_assignment(tmp_path, capsys):
    scen_file = tmp_path / "scope_checks.py"
    scen_file.write_text(
        """import urllib.request
import json
import shape
from shape.fakes import SendGridFake

@shape.effect("demo.send")
def bare_send(msg: str) -> None:
    pass

@shape.scenario(invariants=[shape.at_most_once("demo.send")], max_restarts=0)
def bare_default(run: shape.Run) -> None:
    bare_send("hello")

@shape.scenario(net=[SendGridFake()], max_restarts=0)
def sg_default(run: shape.Run) -> None:
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps({
            "personalizations": [{"to": [{"email": "alice@example.com"}]}],
            "from": {"email": "noreply@acme.test"},
            "subject": "Status",
            "content": [{"type": "text/plain", "value": "ok"}],
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req):
        pass
""",
        encoding="utf-8",
    )

    # Normal mode passes and prints scope assignment + default-scope warning
    code = cli.main([
        "test", f"{scen_file}::sg_default", "--seeds", "1", "--runs-dir", str(tmp_path / "r1"),
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "scope assignment:" in out
    assert "sendgrid.mail.send" in out
    assert "to|subject" in out
    assert "warning:" in out

    # --strict-scopes refuses fake default scope
    code_strict_sg = cli.main([
        "test", f"{scen_file}::sg_default", "--seeds", "1", "--strict-scopes",
        "--runs-dir", str(tmp_path / "r2"),
    ])
    out_strict_sg = capsys.readouterr().out
    assert code_strict_sg == 1
    assert "scope=" in out_strict_sg

    # --strict-scopes refuses @shape.effect default empty scope
    code_strict_eff = cli.main([
        "test", f"{scen_file}::bare_default", "--seeds", "1", "--strict-scopes",
        "--runs-dir", str(tmp_path / "r3"),
    ])
    out_strict_eff = capsys.readouterr().out
    assert code_strict_eff == 1
    assert "scope=" in out_strict_eff
    assert "scope assignment:" in out_strict_eff


# ----------------------------------------------------------- 2. Explicit adapter restart policy


def test_agents_drive_on_restart_none_redeliver_and_callable(tmp_path):
    pytest.importorskip("agents")
    from agents import Agent, function_tool

    import shape.agents

    @function_tool
    def send_notice(job_id: str) -> str:
        """Send a notice."""
        return f"sent:{job_id}"

    def _make_agent():
        tools = shape.agents.effects(
            [send_notice],
            ops={"send_notice": {"op": "notice.send", "scope": lambda job_id, **_: f"job:{job_id}"}},
        )
        model = shape.agents.ScriptedModel([("send_notice", {"job_id": "j1"})], final="done")
        return Agent(name="Notifier", instructions="Send notice.", model=model, tools=tools)

    # Default on_restart="none": restarted incarnation does nothing -> no harness-invented duplicate
    @shape.scenario(invariants=[shape.at_most_once("notice.send")], max_restarts=2)
    def default_no_supervisor(run: shape.Run) -> None:
        shape.agents.drive(_make_agent(), "send j1", run)

    out_none = runner.execute(
        default_no_supervisor,
        {"explicit": {"fault:notice.send#0.0": _fault("crash_after")}},
        tmp_path / "none",
    )
    assert out_none.ok, out_none.violations
    assert len([e for e in out_none.entries if e["kind"] == "effect"]) == 1

    # Explicit on_restart="redeliver": resends input, produces duplicate, logs harness trace line
    @shape.scenario(invariants=[shape.at_most_once("notice.send")], max_restarts=2)
    def explicit_redeliver(run: shape.Run) -> None:
        shape.agents.drive(_make_agent(), "send j1", run, on_restart="redeliver")

    out_redel = runner.execute(
        explicit_redeliver,
        {"explicit": {"fault:notice.send#0.0": _fault("crash_after")}},
        tmp_path / "redel",
    )
    assert out_redel.violated("at_most_once(notice.send)")
    lines, _ = runner.story(out_redel.entries)
    assert any("harness redelivered the request (on_restart='redeliver')" in line for line in lines)

    # Callable on_restart: runs user's custom recovery
    @shape.scenario(invariants=[shape.at_most_once("notice.send")], max_restarts=2)
    def custom_restart(run: shape.Run) -> None:
        def recover(r: shape.Run) -> None:
            (r.dir / "recovered.txt").write_text(f"restart-{r.restarts}")

        shape.agents.drive(_make_agent(), "send j1", run, on_restart=recover)

    out_cb = runner.execute(
        custom_restart,
        {"explicit": {"fault:notice.send#0.0": _fault("crash_after")}},
        tmp_path / "cb",
    )
    assert out_cb.ok, out_cb.violations
    assert (out_cb.dir / "recovered.txt").read_text() == "restart-1"


def test_langgraph_drive_on_restart_policies(tmp_path):
    pytest.importorskip("langgraph")
    pytest.importorskip("langgraph.checkpoint.sqlite")
    from typing import TypedDict

    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, START, StateGraph

    import shape.langgraph

    class S(TypedDict):
        val: str

    @shape.effect("lg.send", scope=lambda val, **_: f"v:{val}")
    def do_send(val: str) -> str:
        return val

    def build(cp):
        b = StateGraph(S)
        b.add_node("send", lambda s: {"val": do_send(s["val"])})
        b.add_edge(START, "send")
        b.add_edge("send", END)
        return b.compile(checkpointer=cp)

    # on_restart="none" does nothing on restart
    @shape.scenario(invariants=[shape.at_most_once("lg.send")], max_restarts=2)
    def lg_none(run: shape.Run) -> None:
        with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as cp:
            shape.langgraph.drive(
                build(cp), {"val": "1"}, {"configurable": {"thread_id": "t1"}},
                run, durability="sync", on_restart="none",
            )

    out_none = runner.execute(
        lg_none,
        {"explicit": {"fault:lg.send#0.0": _fault("crash_after")}},
        tmp_path / "lg_none",
    )
    assert out_none.ok, out_none.violations
    assert len([e for e in out_none.entries if e["kind"] == "effect"]) == 1

    # on_restart="redeliver" resends input and logs the harness redelivery line
    @shape.scenario(invariants=[shape.at_most_once("lg.send")], max_restarts=2)
    def lg_redeliver(run: shape.Run) -> None:
        with SqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as cp:
            shape.langgraph.drive(
                build(cp), {"val": "1"}, {"configurable": {"thread_id": "t1"}},
                run, durability="sync", on_restart="redeliver",
            )

    out_redel = runner.execute(
        lg_redeliver,
        {"explicit": {"fault:lg.send#0.0": _fault("crash_after")}},
        tmp_path / "lg_redel",
    )
    assert out_redel.violated("at_most_once(lg.send)")
    lines, _ = runner.story(out_redel.entries)
    assert any("harness redelivered the request (on_restart='redeliver')" in line for line in lines)


# ----------------------------------------------------------- 3. Distinct after-commit triggers


def test_reply_lost_and_crash_after_are_distinct_trigger_classes(tmp_path):
    assert fault_class("timeout_after_commit") == "reply_lost"
    assert fault_class("connection_reset_after_commit") == "reply_lost"
    assert fault_class("crash_after") == "crash_after"
    assert fault_class("timeout") == "before_commit"
    assert fault_class("crash_before") == "crash_before"

    swept = runner.run_sweep(sc.naive_retry, range(300), tmp_path)
    report = runner.investigate_all(sc.naive_retry, swept.failing, tmp_path, "x")
    defect, = report.defects
    assert {"reply_lost", "crash_after"} <= defect.triggers.keys()
    assert defect.triggers["reply_lost"].faults == {
        "timeout_after_commit", "connection_reset_after_commit",
    }
    assert defect.triggers["crash_after"].faults == {"crash_after"}


# ----------------------------------------------------------- 4. SmtpFake fresh sends invariant


def test_smtp_fake_default_invariants_cover_fresh_sends(tmp_path):
    msg = EmailMessage()
    msg["From"] = "bot@acme.test"
    msg["To"] = "alice@example.com"
    msg["Subject"] = "Weekly digest"
    msg["X-Job-Id"] = "job-1"
    msg.set_content("Hello Alice")

    # Duplicate fresh send (no In-Reply-To) must violate SmtpFake default invariant at_most_once(smtp.send)
    @shape.scenario(net=[SmtpFake()], max_restarts=0)
    def duplicate_fresh_send(run: shape.Run) -> None:
        with smtplib.SMTP_SSL("smtp.example.com") as smtp:
            smtp.send_message(msg)
            smtp.send_message(msg)

    out_dup = runner.execute(duplicate_fresh_send, {"explicit": {}}, tmp_path / "dup")
    assert out_dup.violated("at_most_once(smtp.send)")

    # Under strict_scopes=True, default (to, subject) scope is refused
    @shape.scenario(net=[SmtpFake()], max_restarts=0, strict_scopes=True)
    def fresh_send_strict_default(run: shape.Run) -> None:
        with smtplib.SMTP_SSL("smtp.example.com") as smtp:
            smtp.send_message(msg)

    out_strict = runner.execute(fresh_send_strict_default, {"explicit": {}}, tmp_path / "strict")
    assert out_strict.violated("at_most_once(smtp.send)")
    assert "scope=" in out_strict.violations[0].message

    # Configurable scope= distinguishes two jobs with same recipient + subject and passes strict mode
    @shape.scenario(
        net=[SmtpFake(scope=lambda req, parsed: f"job:{parsed['headers']['x-job-id']}")],
        max_restarts=0,
        strict_scopes=True,
    )
    def two_fresh_jobs_explicit(run: shape.Run) -> None:
        msg2 = EmailMessage()
        msg2["From"] = "bot@acme.test"
        msg2["To"] = "alice@example.com"
        msg2["Subject"] = "Weekly digest"
        msg2["X-Job-Id"] = "job-2"
        msg2.set_content("Hello again")
        with smtplib.SMTP_SSL("smtp.example.com") as smtp:
            smtp.send_message(msg)
            smtp.send_message(msg2)

    out_ok = runner.execute(two_fresh_jobs_explicit, {"explicit": {}}, tmp_path / "ok")
    assert out_ok.ok, out_ok.violations
    assert out_ok.stats["effects"] == {"smtp.send": 2}


# ----------------------------------------------------------- 5. Distinct session per drive() job


def test_agents_drive_uses_distinct_session_per_job_and_supports_explicit_sharing(tmp_path):
    pytest.importorskip("agents")
    from agents import Agent, function_tool

    import shape.agents

    @function_tool
    def send_email(job_id: str) -> str:
        """Send an email for a job."""
        return f"sent:{job_id}"

    def _agent_for(job_id: str):
        tools = shape.agents.effects(
            [send_email],
            ops={"send_email": {"op": "mail.send", "scope": lambda job_id, **_: f"job:{job_id}"}},
        )
        model = shape.agents.ScriptedModel([("send_email", {"job_id": job_id})], final=f"done {job_id}")
        return Agent(name="Mailer", instructions="Send email.", model=model, tools=tools)

    # Two independent jobs in one scenario must NOT share a session by default (both must call tool)
    @shape.scenario(invariants=[shape.at_most_once("mail.send")], max_restarts=0)
    def two_independent_jobs(run: shape.Run) -> None:
        shape.agents.drive(_agent_for("job-1"), "Send job 1", run)
        shape.agents.drive(_agent_for("job-2"), "Send job 2", run)

    out = runner.execute(two_independent_jobs, {"explicit": {}}, tmp_path / "two_jobs")
    assert out.ok, out.violations
    scopes = [e["scope"] for e in out.entries if e["kind"] == "effect"]
    assert scopes == ["job:job-1", "job:job-2"]

    # Deliberately sharing a session via session_id= lets the second turn see the first turn's history
    @shape.scenario(invariants=[shape.at_most_once("mail.send")], max_restarts=0)
    def shared_session_turns(run: shape.Run) -> None:
        shape.agents.drive(_agent_for("job-1"), "Send job 1", run, session_id="conv-1")
        shape.agents.drive(_agent_for("job-1"), "Follow up", run, session_id="conv-1")

    out_shared = runner.execute(shared_session_turns, {"explicit": {}}, tmp_path / "shared")
    assert out_shared.ok, out_shared.violations
    # Second turn saw send_email already completed in shared session history
    assert len([e for e in out_shared.entries if e["kind"] == "effect"]) == 1


def test_strict_scopes_covers_exactly_once_and_stripe_metadata_key(tmp_path):
    requests = pytest.importorskip("requests")

    @shape.effect("demo.bare_exact")
    def bare_exact() -> None:
        pass

    @shape.scenario(invariants=[shape.exactly_once("demo.bare_exact")], max_restarts=1)
    def exact_default(run: shape.Run) -> None:
        bare_exact()

    # shape.check(..., strict_scopes=True) refuses default scope for exactly_once
    with pytest.raises(AssertionError) as info:
        shape.check(exact_default, seeds=1, runs_dir=tmp_path / "exact", strict_scopes=True)
    text = str(info.value)
    assert "strict_scopes=True" in text
    assert "scope=" in text
    assert "scope assignment:" in text

    url = "https://api.stripe.com/v1/charges"
    data = {"amount": 500, "currency": "usd", "source": "tok_visa", "metadata[order_id]": "42"}

    # Default StripeFake() is refused under strict_scopes=True
    @shape.scenario(net=[shape.fakes.StripeFake()], max_restarts=0, strict_scopes=True)
    def stripe_default_strict(run: shape.Run) -> None:
        requests.post(url, data=data, headers={"Idempotency-Key": "k1"}).raise_for_status()

    out_def = runner.execute(stripe_default_strict, {"explicit": {}}, tmp_path / "st_def")
    assert out_def.violated("at_most_once(stripe.charge)")
    assert "metadata.order_id" in out_def.violations[0].message

    # StripeFake(scope_metadata_key="order_id") is explicit and passes strict_scopes=True
    @shape.scenario(
        net=[shape.fakes.StripeFake(scope_metadata_key="order_id")],
        max_restarts=0,
        strict_scopes=True,
    )
    def stripe_explicit_strict(run: shape.Run) -> None:
        requests.post(url, data=data, headers={"Idempotency-Key": "k1"}).raise_for_status()

    out_exp = runner.execute(stripe_explicit_strict, {"explicit": {}}, tmp_path / "st_exp")
    assert out_exp.ok, out_exp.violations


def test_drive_validates_on_restart_and_adrive_supports_policies(tmp_path):
    pytest.importorskip("agents")
    pytest.importorskip("langgraph")
    pytest.importorskip("langgraph.checkpoint.sqlite")
    from typing import TypedDict

    from agents import Agent, SQLiteSession
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.graph import END, START, StateGraph

    import shape.agents
    import shape.langgraph

    agent = Agent(
        name="A", instructions="test", model=shape.agents.ScriptedModel([], final="ok"), tools=[]
    )
    dummy_run = shape.Run(tmp_path, 0)
    with pytest.raises(ValueError, match="on_restart"):
        shape.agents.drive(agent, "hi", dummy_run, on_restart="bogus")
    with pytest.raises(ValueError, match="session_id"):
        shape.agents.drive(
            agent,
            "hi",
            dummy_run,
            session_id="s1",
            session_factory=lambda d: SQLiteSession("s2", str(d / "s.db")),
        )

    class S(TypedDict):
        v: str

    @shape.effect("lg.asend", scope=lambda v, **_: f"v:{v}")
    async def asend(v: str) -> str:
        await asyncio.sleep(0)
        return v

    async def _run_async(run: shape.Run, on_restart):
        async with AsyncSqliteSaver.from_conn_string(str(run.dir / "ckpt.db")) as cp:
            b = StateGraph(S)

            async def node(s: S) -> dict:
                return {"v": await asend(s["v"])}

            b.add_node("send", node)
            b.add_edge(START, "send")
            b.add_edge("send", END)
            g = b.compile(checkpointer=cp)
            return await shape.langgraph.adrive(
                g, {"v": "1"}, {"configurable": {"thread_id": "t1"}}, run, on_restart=on_restart
            )

    @shape.scenario(invariants=[shape.at_most_once("lg.asend")], max_restarts=1)
    def adrive_redeliver(run: shape.Run) -> None:
        asyncio.run(_run_async(run, "redeliver"))

    out = runner.execute(
        adrive_redeliver,
        {"explicit": {"fault:lg.asend#0.0": _fault("crash_after")}},
        tmp_path / "adrive",
    )
    assert out.violated("at_most_once(lg.asend)")
    lines, _ = runner.story(out.entries)
    assert any("harness redelivered the request (on_restart='redeliver')" in line for line in lines)

    @shape.scenario(invariants=[shape.at_most_once("lg.asend")], max_restarts=1)
    def adrive_callable(run: shape.Run) -> None:
        async def recover(r: shape.Run) -> None:
            (r.dir / "async_rec.txt").write_text(f"ok-{r.restarts}")

        asyncio.run(_run_async(run, recover))

    out_cb = runner.execute(
        adrive_callable,
        {"explicit": {"fault:lg.asend#0.0": _fault("crash_after")}},
        tmp_path / "adrive_cb",
    )
    assert out_cb.ok, out_cb.violations
    assert (out_cb.dir / "async_rec.txt").read_text() == "ok-1"

