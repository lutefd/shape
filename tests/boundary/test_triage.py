"""Regression tests for triage and report quality fixes:
1. Nominal-path failures do not hide distinct fault-driven failures under the same defect.
2. Only one representative per (defect, trigger class) is minimized and written to `.min.json`.
3. Minimized trace trigger labels list all kept deviations in chronological order.
4. FLAKY seeds preserve confirmation reruns under `runs/<scenario>/flaky/seed-N/run-K/`
   and report the first divergence between reruns.
5. Noise reduction: uncalled-op `note:` lines are hidden unless `--verbose`, and user-registered
   fakes that only return without effect are omitted from `"effect default"` scope listings.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import scenarios as sc

import shape
from shape import cli
from shape.boundary import runner
from shape.boundary.faults import FAULTS
from shape.fakes.base import EffectInfo, Response


def _fault(name: str) -> int:
    return FAULTS.index(name) + 1


# ----------------------------------------------- 1. Nominal failure does not hide fault defects


@shape.effect("t.triage_pay", scope=lambda order, **_: f"order:{order}")
def triage_pay(order: str, attempt: str = "init") -> None:
    pass


@shape.scenario(invariants=[shape.at_most_once("t.triage_pay")], max_restarts=1)
def nominal_and_fault_same_invariant(run: shape.Run) -> None:
    """Fails at_most_once nominally, AND fails differently on crash_after and reply_lost."""
    if run.restarts == 0:
        try:
            triage_pay("1", attempt="first")
        except (TimeoutError, ConnectionResetError):
            triage_pay("1", attempt="retry_after_lost_reply")
        triage_pay("1", attempt="nominal_dup")
    else:
        triage_pay("1", attempt="restart_dup")


def test_nominal_failure_does_not_hide_fault_driven_defects(tmp_path):
    swept = runner.run_sweep(nominal_and_fault_same_invariant, range(200), tmp_path)
    report = runner.investigate_all(
        nominal_and_fault_same_invariant,
        swept.failing,
        tmp_path,
        "nominal_and_fault_same_invariant",
        defect_keys=swept.defect_keys,
    )
    defect, = report.defects
    assert defect.key.invariant == "at_most_once(t.triage_pay)"
    assert "nominal" in defect.triggers
    assert "crash_after" in defect.triggers
    assert "reply_lost" in defect.triggers

    text = report.render()  # default rendering, WITHOUT all_triggers=True
    assert "nominal path fails;" in text
    assert "fault-driven" in text
    assert "crash after commit replay:" in text
    assert "reply lost replay:" in text
    assert "restart_dup" in text
    assert "retry_after_lost_reply" in text


# ------------------------------ 2. Minimize only one representative per (defect, trigger class)


def test_only_representatives_are_minimized_and_written_to_min_json(tmp_path):
    runs = tmp_path / "naive"
    swept = runner.run_sweep(sc.naive_retry, range(200), runs)
    assert len(swept.failing) > 10
    report = runner.investigate_all(
        sc.naive_retry, swept.failing, runs, "naive_retry", defect_keys=swept.defect_keys,
    )
    defect, = report.defects
    assert len(defect.seeds) == len(swept.failing)
    assert {"reply_lost", "crash_after"} <= set(defect.triggers)

    rep_paths = {t.failure.replay_file for t in defect.triggers.values()}
    if defect.failure is not None:
        rep_paths.add(defect.failure.replay_file)
    min_files = set((runs / "naive_retry").glob("*.min.json"))
    assert min_files == rep_paths
    assert len(min_files) <= len(defect.triggers)
    assert len(min_files) < len(swept.failing)

    # Every written representative .min.json still replays to the same violation.
    for path in min_files:
        code, _ = runner.replay_file(sc.naive_retry, path, runs)
        assert code == 1

    # Also verify a nominal-failing sweep writes only 1 .min.json across 20 failing seeds.
    nom_runs = tmp_path / "nominal"
    nom_swept = runner.run_sweep(sc.mail_without_approval, range(20), nom_runs)
    assert len(nom_swept.failing) == 20
    nom_report = runner.investigate_all(
        sc.mail_without_approval,
        nom_swept.failing,
        nom_runs,
        "mail_without_approval",
        defect_keys=nom_swept.defect_keys,
    )
    nom_defect, = nom_report.defects
    assert len(nom_defect.seeds) == 20
    assert len(list((nom_runs / "mail_without_approval").glob("*.min.json"))) == 1


# ------------------------------------ 3. Minimized trace trigger labels list all kept deviations


class _MailWorkflowFake:
    hosts = ("api.workflow.invalid",)
    api_version = "test"

    def handle(self, req, state):
        if req.url.endswith("/prepare"):
            state["prepared"] = True
            return Response(200, {"Content-Type": "application/json"}, b'{"ok":true}')
        if req.url.endswith("/send"):
            state["sent"] = state.get("sent", 0) + 1
            return Response(202, {"Content-Type": "application/json"}, b'{"ok":true}')
        return Response(404, {}, b"")

    def describe(self, req):
        if req.url.endswith("/prepare"):
            return EffectInfo("workflow.prepare", "job:1", None, False, False)
        if req.url.endswith("/send"):
            return EffectInfo("workflow.email", "job:1", None, True, False)
        return None

    def effect(self, req, resp):
        if req.url.endswith("/prepare"):
            return EffectInfo("workflow.prepare", "job:1", None, False, resp.status == 200)
        if req.url.endswith("/send"):
            return EffectInfo("workflow.email", "job:1", None, True, resp.status == 202)
        return None

    def invariants(self):
        return []


def _post(url: str) -> int:
    req = urllib.request.Request(
        url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


@shape.scenario(
    net=[_MailWorkflowFake()],
    invariants=[shape.exactly_once("workflow.email")],
    max_restarts=2,
)
def crash_then_429_loses_email(run: shape.Run) -> None:
    """Incarnation 0 prepares then sends (with retry). If inc 0 crashes after prepare,
    incarnation 1 enters a recovery path that drops HTTP 429 without retrying."""
    if run.restarts == 0:
        _post("https://api.workflow.invalid/prepare")
        for _ in range(3):
            if _post("https://api.workflow.invalid/send") == 202:
                return
    else:
        # Recovery path: fails to retry 429, losing the email.
        status = _post("https://api.workflow.invalid/send")
        if status == 429:
            return
        if status != 202:
            _post("https://api.workflow.invalid/send")


def test_minimized_trace_label_includes_all_kept_deviations_in_order(tmp_path):
    # Needs BOTH crash_after in inc 0 AND HTTP 429 (rate_limit) in inc 1.
    realized = {
        "fault:workflow.prepare#0.0": _fault("crash_after"),
        "fault:workflow.email#1.0": _fault("rate_limit"),
    }
    out = runner.execute(crash_then_429_loses_email, {"explicit": realized, "seed": 7},
                         tmp_path / "crash_then_429_loses_email" / "seed-7")
    assert out.violated("exactly_once(workflow.email)")

    report = runner.investigate_all(
        crash_then_429_loses_email,
        [(7, "exactly_once(workflow.email)", realized)],
        tmp_path,
        "crash_then_429_loses_email",
    )
    defect, = report.defects
    failure = defect.failure
    assert failure is not None
    assert failure.deviations == 2
    assert failure.trigger_label == "crash after commit + before-commit fault (429)"
    text = report.render()
    assert "trigger: crash after commit + before-commit fault (429)" in text
    assert json.loads(failure.replay_file.read_text())["realized"] == realized


# ------------------------------------ 4. FLAKY seeds preserve confirmation reruns and divergence


def test_flaky_seeds_keep_confirmation_reruns_and_report_first_divergence(tmp_path):
    counter = tmp_path / "count"

    @shape.scenario(invariants=[shape.at_most_once("t.pay")], max_restarts=0)
    def flaky_with_logs(run: shape.Run) -> None:
        n = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(n + 1))
        print(f"execution #{n}")
        sc.pay_unkeyed_twice()
        if n == 0:
            sc.pay_unkeyed_twice()

    runs = tmp_path / "runs"
    swept = runner.run_sweep(flaky_with_logs, range(1), runs)
    assert swept.failing == []
    assert swept.flaky == [0]

    flaky_seed_dir = runs / "flaky_with_logs" / "flaky" / "seed-0"
    for k in range(runner.RERUNS + 1):
        run_dir = flaky_seed_dir / f"run-{k}"
        assert run_dir.is_dir(), f"missing {run_dir}"
        assert (run_dir / "inc-0.out").is_file()
        assert (run_dir / "world.jsonl").is_file()
        assert (run_dir / "realized.jsonl").is_file()
    assert "execution #0" in (flaky_seed_dir / "run-0" / "inc-0.out").read_text()
    assert "execution #1" in (flaky_seed_dir / "run-1" / "inc-0.out").read_text()

    rendered = runner.render_flaky(swept.flaky, swept.flaky_details)
    assert str(flaky_seed_dir) in rendered
    assert "first divergence (run-0 vs run-1)" in rendered
    assert "pay_unkeyed_twice()" in rendered
    assert "finished" in rendered


# ------------------------------ 5. Noise: uncalled invariant notes + no-effect fake default scope


def test_noise_uncalled_invariant_notes_hidden_by_default_and_no_effect_fake_omitted(
    tmp_path, capsys,
):
    scen_file = tmp_path / "noise_checks.py"
    scen_file.write_text(
        """import smtplib
import urllib.request
from email.message import EmailMessage

import shape
from shape.fakes import SmtpFake
from shape.fakes.base import Response


class LlmStubFake:
    hosts = ("llm.invalid",)
    api_version = "test"

    def handle(self, req, state):
        return Response(200, {"Content-Type": "application/json"}, b'{"reply":"ok"}')

    def effect(self, req, resp):
        return None

    def invariants(self):
        return []


@shape.scenario(
    net=[SmtpFake(scope=lambda req, parsed: f"job:{parsed['headers']['x-job-id']}"), LlmStubFake()],
    max_restarts=2,
)
def send_only_with_llm_fake(run: shape.Run) -> None:
    req = urllib.request.Request(
        "https://llm.invalid/v1/chat",
        data=b'{"prompt":"hi"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req):
            pass
    except Exception:
        pass
    msg = EmailMessage()
    msg["From"] = "bot@acme.test"
    msg["To"] = "alice@example.com"
    msg["Subject"] = "Hello"
    msg["X-Job-Id"] = "job-1"
    msg.set_content("Hi")
    try:
        with smtplib.SMTP_SSL("smtp.example.com") as smtp:
            smtp.send_message(msg)
    except Exception:
        pass
""",
        encoding="utf-8",
    )

    # Default CLI output: no "note: an invariant watches smtp.reply" noise, and no
    # "POST llm.invalid/v1/chat: effect default" under scope assignment.
    code = cli.main([
        "test", f"{scen_file}::send_only_with_llm_fake", "--seeds", "5",
        "--runs-dir", str(tmp_path / "r1"),
    ])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "note: an invariant watches smtp.reply" not in out
    assert "POST llm.invalid/v1/chat: effect default" not in out
    assert "smtp.send: explicit scope=" in out

    # --verbose restores the uncalled-op diagnostic note.
    code_v = cli.main([
        "test", f"{scen_file}::send_only_with_llm_fake", "--seeds", "5", "--verbose",
        "--runs-dir", str(tmp_path / "r2"),
    ])
    out_v = capsys.readouterr().out
    assert code_v == 0, out_v
    assert "note: an invariant watches smtp.reply, which no seed ever called" in out_v
    assert "POST llm.invalid/v1/chat: effect default" not in out_v


def test_describe_triggers_orders_all_kept_deviations():
    from shape.boundary.failure import describe_triggers

    assert describe_triggers([]) == "nominal (a plain unit test catches this)"
    entries = [
        {"kind": "fault", "fault": "rate_limit", "raised": "HTTP 429", "inc": 1, "seq": 1},
        {"kind": "fault", "fault": "crash_after", "raised": None, "inc": 0, "seq": 2},
        {"kind": "choice", "choice": "retry", "inc": 1, "seq": 2},
    ]
    assert describe_triggers(entries) == (
        "crash after commit + before-commit fault (429) + model choice"
    )
    non_http = [
        {"kind": "fault", "fault": "duplicate_trigger", "raised": None, "inc": 0, "seq": 0},
        {"kind": "schedule", "schedule": "actors", "inc": 0, "seq": 1},
        {"kind": "fault", "fault": "rate_limit", "raised": "RateLimited", "inc": 0, "seq": 2},
    ]
    assert describe_triggers(non_http) == (
        "duplicate trigger + interleaving + before-commit fault (rate_limit)"
    )


def test_single_fault_seed_still_reports_both_nominal_and_fault_driven_failure(tmp_path):
    # Even when a sweep has only 1 seed and that seed has a fault, shrinking to nominal
    # must not hide the distinct fault-driven failure.
    realized = {"fault:t.triage_pay#0.0": _fault("crash_after")}
    out = runner.execute(
        nominal_and_fault_same_invariant,
        {"explicit": realized, "seed": 42},
        tmp_path / "nominal_and_fault_same_invariant" / "seed-42",
    )
    assert out.violated("at_most_once(t.triage_pay)")
    report = runner.investigate_all(
        nominal_and_fault_same_invariant,
        [(42, "at_most_once(t.triage_pay)", realized)],
        tmp_path,
        "nominal_and_fault_same_invariant",
    )
    defect, = report.defects
    assert {"nominal", "crash_after"} <= set(defect.triggers)
    nom_file = defect.triggers["nominal"].failure.replay_file
    crash_file = defect.triggers["crash_after"].failure.replay_file
    assert nom_file != crash_file
    assert json.loads(nom_file.read_text())["realized"] == {}
    assert json.loads(crash_file.read_text())["realized"] == realized
    text = report.render()
    assert "nominal path fails; 1 fault-driven seed also fails;" in text
    assert "trigger: crash after commit" in text
