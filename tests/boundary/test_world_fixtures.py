"""World fixtures: a fault-free setup phase, per-seed resources, a virtual clock, and effects that
are at-most-once by design."""

import contextlib
import dataclasses
import datetime
import json
import os
import sqlite3
import time
import urllib.request

import pytest

import shape
from shape.boundary import runner
from shape.boundary.faults import FAULTS
from shape.fakes.base import EffectInfo, Response


def note(run, **values):
    """Scenarios run in forked processes: they leave notes in the run directory for the test."""
    with open(run.dir / "notes.jsonl", "a") as f:
        f.write(json.dumps(values) + "\n")


def notes(workdir):
    lines = (workdir / "notes.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


# ------------------------------------------------------------------------------------------ setup


@shape.effect("seed.insert", scope=lambda row: f"row:{row}")
def insert_seed(row):
    pass


@shape.effect("jobs.write", scope=lambda job: f"job:{job}")
def write(job):
    pass


def test_setup_runs_without_faults_and_is_logged_as_setup(tmp_path):
    @shape.scenario(setup=lambda run: [insert_seed(1), insert_seed(2)],
                    invariants=[shape.at_most_once("seed.insert")], max_restarts=1)
    def job(run):
        write(1)

    explicit = {"fault:seed.insert#-1a.0": FAULTS.index("crash_before") + 1}
    out = runner.execute(job, {"explicit": explicit}, tmp_path)
    assert out.ok
    setup = [e for e in out.entries if e.get("setup")]
    assert [e["call"] for e in setup if e["kind"] == "effect"] == [
        "insert_seed(row=1)", "insert_seed(row=2)"]
    assert all(e["fault"] is None for e in setup if e["kind"] == "effect")
    lines, steps = runner.story(out.entries)
    assert lines[0] == "setup (no faults)"
    assert lines[1:3] == ["  insert_seed(row=1)", "  insert_seed(row=2)"]
    assert steps == 1  # setup is not a numbered step


def test_setup_effects_are_not_counted_unless_asked(tmp_path):
    seen = {}

    @shape.invariant
    def inspect_world(world):
        seen["default"] = len(world.effects("seed.insert"))
        seen["asked"] = len(world.effects("seed.insert", include_setup=True))
        seen["attempts"] = len(world.attempts("seed.insert"))
        seen["setup_entries"] = len(world.setup)

    @shape.scenario(setup=lambda run: (insert_seed(1), insert_seed(1)), invariants=[inspect_world])
    def job(run):
        write(1)

    assert runner.execute(job, {"explicit": {}}, tmp_path).ok
    assert seen == {"default": 0, "asked": 2, "attempts": 0, "setup_entries": 4}


def test_a_scenario_that_only_calls_setup_effects_tests_nothing(tmp_path):
    @shape.scenario(setup=lambda run: insert_seed(1), invariants=[shape.at_most_once("seed.insert")])
    def job(run):
        pass

    stats = runner.execute(job, {"explicit": {}}, tmp_path).stats
    assert stats["effects"] == {} and stats["attempts"] == {}


def test_setup_gets_no_model_choices(tmp_path):
    @shape.scenario(setup=lambda run: note(run, picked=shape.choose("who", ["nominal", "evil"],
                                                                    weights=[0, 1])))
    def job(run):
        pass

    runner.execute(job, {"seed": 1}, tmp_path)
    assert notes(tmp_path) == [{"picked": "nominal"}]


def test_setup_that_raises_is_a_harness_error(tmp_path):
    def broken(run):
        raise RuntimeError("no database")

    @shape.scenario(setup=broken)
    def job(run):
        write(1)

    out = runner.execute(job, {"explicit": {}}, tmp_path)
    assert [v.invariant for v in out.violations] == ["harness_error"]
    assert "your setup raised RuntimeError: no database" in out.violations[0].message
    assert not (tmp_path / "inc-0.out").exists()  # no incarnation started


def test_setup_network_calls_hit_the_fake_without_faults(tmp_path):
    class Fake:
        hosts = ("seed.invalid",)
        api_version = "test"

        def handle(self, req, state):
            state.setdefault("n", 0)
            if req.method == "POST":
                state["n"] += 1
            return Response(200, {}, json.dumps({"n": state["n"]}).encode())

        def describe(self, req):
            return EffectInfo("seed.post", "s", None, False, False) if req.method == "POST" else None

        def effect(self, req, resp):
            return EffectInfo("seed.post", "s", None, False, True) if req.method == "POST" else None

        def invariants(self):
            return []

    def post():
        request = urllib.request.Request("https://seed.invalid/", data=b"{}", method="POST")
        return json.load(urllib.request.urlopen(request))["n"]

    @shape.scenario(net=[Fake()], setup=lambda run: note(run, setup_saw=post()), max_restarts=0)
    def job(run):
        note(run, job_saw=post())

    explicit = {"fault:seed.post#-1a.0": FAULTS.index("server_error") + 1}
    out = runner.execute(job, {"explicit": explicit}, tmp_path)
    assert out.ok
    assert [e["kind"] for e in out.entries if e.get("setup")] == ["attempt", "effect"]
    assert out.stats["effects"] == {"seed.post": 1}  # only the scenario's own call counts
    assert notes(tmp_path) == [{"setup_saw": 1}, {"job_saw": 2}]  # one fake, one shared state


def test_fakes_start_from_initial_state(tmp_path):
    class Fake:
        hosts = ("seed.invalid",)
        api_version = "test"

        def handle(self, req, state):
            return Response(200, {}, json.dumps(state.get("rows", [])).encode())

        def invariants(self):
            return []

    shape.fakes.seedable(Fake)

    @shape.scenario(net=[Fake(initial_state={"rows": ["a", "b"]})], max_restarts=0,
                    expect_effects=False)
    def job(run):
        note(run, rows=json.load(urllib.request.urlopen("https://seed.invalid/")))

    assert runner.execute(job, {"explicit": {}}, tmp_path).ok
    assert notes(tmp_path) == [{"rows": ["a", "b"]}]


def test_langgraph_fake_starts_with_threads(tmp_path):
    fake = shape.fakes.LangGraphFake(threads=[{"metadata": {"user": "u1"}, "values": {"n": 1}}])
    [thread_id] = fake.initial_state["threads"]

    @shape.scenario(net=[fake], max_restarts=0, expect_effects=False)
    def job(run):
        url = f"https://langgraph.example.com/threads/{thread_id}"
        note(run, thread=json.load(urllib.request.urlopen(url)))

    assert runner.execute(job, {"explicit": {}}, tmp_path).ok
    [seen] = notes(tmp_path)
    assert seen["thread"]["metadata"] == {"user": "u1"} and seen["thread"]["values"] == {"n": 1}


# --------------------------------------------------------------------------------------- fixtures


def make_schema(path):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, state TEXT)")


@shape.invariant
def one_row_per_seed(world):
    path = world.run.fixture("db")
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    return None if rows == 1 else f"{rows} rows in {path}: seeds shared a database"


def one_row_job(run):
    time.sleep(0.01)
    with sqlite3.connect(run.fixture("db")) as db:  # a restart repeats this: keyed by the seed
        db.execute("INSERT OR REPLACE INTO jobs (id, state) VALUES (?, 'done')", (run.seed,))
    write(1)


def test_sqlite_fixture_gives_every_seed_its_own_database_under_jobs(tmp_path):
    scenario = shape.scenario(one_row_job, fixtures=[shape.fixtures.sqlite_copy(init=make_schema)],
                              invariants=[one_row_per_seed])
    shape.check(scenario, seeds=24, jobs=4, runs_dir=tmp_path)


def test_sqlite_copy_clones_a_template_file(tmp_path):
    template = tmp_path / "template.sqlite3"
    make_schema(template)
    with sqlite3.connect(template) as db:
        db.execute("INSERT INTO jobs (state) VALUES ('seeded')")

    def check_rows(run):
        with sqlite3.connect(run.fixture("db")) as db:
            assert ("seeded",) in db.execute("SELECT state FROM jobs").fetchall()
            db.execute("INSERT OR REPLACE INTO jobs (id, state) VALUES (1000, 'mine')")
        write(1)

    scenario = shape.scenario(check_rows, fixtures=[shape.fixtures.sqlite_copy(template)])
    shape.check(scenario, seeds=5, jobs=2, runs_dir=tmp_path / "runs")
    with sqlite3.connect(template) as db:  # the copies never wrote to the template
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_fixture_is_torn_down_after_the_invariants_read_it(tmp_path):
    events = []

    @shape.fixture
    def resource(run):
        events.append("setup")
        yield {"name": run.unique("res")}
        events.append("teardown")

    @shape.invariant
    def reads_resource(world):
        events.append(f"invariant sees {world.run.fixture('resource')['name'][:4]}")

    @shape.scenario(fixtures=[resource], invariants=[reads_resource])
    def job(run):
        write(1)

    runner.execute(job, {"explicit": {}}, tmp_path)
    assert events == ["setup", "invariant sees res_", "teardown"]


def test_fixture_that_raises_is_a_harness_error_and_still_tears_down_earlier_ones(tmp_path):
    torn = []

    @shape.fixture
    def first(run):
        yield "one"
        torn.append("first")

    @shape.fixture
    def second(run):
        raise RuntimeError("cannot create the database")
        yield

    @shape.scenario(fixtures=[first, second])
    def job(run):
        write(1)

    out = runner.execute(job, {"explicit": {}}, tmp_path)
    assert [v.invariant for v in out.violations] == ["harness_error"]
    assert "your fixture raised RuntimeError: cannot create the database" in out.violations[0].message
    assert torn == ["first"]


def test_fixture_must_yield_json(tmp_path):
    @shape.fixture
    def connection(run):
        yield object()

    @shape.scenario(fixtures=[connection])
    def job(run):
        write(1)

    out = runner.execute(job, {"explicit": {}}, tmp_path)
    assert "JSON" in out.violations[0].message


def test_unavailable_fixture_skips_the_sweep(tmp_path):
    missing = shape.Fixture("pg", lambda run: iter(["never"]), lambda: "no server on :5432")
    scenario = shape.scenario(lambda run: write(1), fixtures=[missing])
    with pytest.raises(shape.FixtureUnavailable, match="no server on :5432"):
        shape.check(scenario, seeds=2, runs_dir=tmp_path)


def test_duplicate_fixture_names_are_rejected():
    with pytest.raises(ValueError, match="two fixtures are named 'db'"):
        shape.scenario(lambda run: None, fixtures=[shape.fixtures.sqlite_copy(init=print),
                                                   shape.fixtures.sqlite_copy(init=print)])


# ----------------------------------------------------------------------------------------- clock


def test_virtual_clock_moves_only_when_advanced(tmp_path):
    @shape.scenario(max_restarts=0, expect_effects=False)
    def job(run):
        note(run, now=shape.clock.now().isoformat())
        time.sleep(0.01)
        note(run, now=shape.clock.now().isoformat())
        run.advance(90)
        note(run, now=shape.clock.now().isoformat())
        shape.clock.sleep(30)  # a virtual sleep: no waiting
        note(run, now=run.now().isoformat())

    assert runner.execute(job, {"explicit": {}}, tmp_path).ok
    start = shape.clock.EPOCH
    assert [n["now"] for n in notes(tmp_path)] == [
        start.isoformat(), start.isoformat(),
        (start + datetime.timedelta(seconds=90)).isoformat(),
        (start + datetime.timedelta(seconds=120)).isoformat()]


def test_clock_is_real_outside_a_run():
    assert abs((shape.clock.now() - datetime.datetime.now(datetime.UTC)).total_seconds()) < 5
    with pytest.raises(RuntimeError):
        shape.clock.advance(1)


def test_advance_on_restart_and_trace(tmp_path):
    @shape.scenario(max_restarts=1, recovery="redeliver", advance_on_restart=300,
                    invariants=[shape.at_most_once("jobs.write")])
    def job(run):
        note(run, now=shape.clock.now().timestamp())
        write(1)
        if not run.restarts:
            os._exit(137)

    out = runner.execute(job, {"explicit": {}}, tmp_path)
    first, second = [n["now"] for n in notes(tmp_path)]
    assert second - first == 300
    lines, _ = runner.story(out.entries)
    assert "  time passed: 300s (clock 2025-01-01T00:05:00Z)" in lines


def test_lease_takeover_waits_for_the_lease_to_expire(tmp_path):
    """The reclaim query uses shape.clock.now(); without time passing, the lease would still hold."""
    def reclaim(run):
        path = run.dir / "lease.json"
        now = shape.clock.now().timestamp()
        held = json.loads(path.read_text()) if path.exists() else None
        if held and held["until"] > now:
            raise RuntimeError(f"{held['by']} still holds the lease")
        path.write_text(json.dumps({"by": run.worker, "until": now + 30}))
        note(run, worker=run.worker)
        write(1)
        if not run.restarts:
            os._exit(137)

    short = shape.scenario(reclaim, max_restarts=1,
                           recovery=shape.recovery.LeaseTakeover(lease_seconds=5, reclaim_interval=0))
    assert runner.execute(short, {"explicit": {}}, tmp_path / "short").violated(
        "scenario_terminates")  # the lease (30s) outlives the 5s the policy waited
    patient = dataclasses.replace(short, recovery="lease-takeover")  # waits 30 seconds
    out = runner.execute(patient, {"explicit": {}}, tmp_path / "patient")
    assert out.ok
    assert [n["worker"] for n in notes(tmp_path / "patient")] == ["worker-0", "worker-1"]


def test_patched_swaps_a_module_clock_and_restores_it(tmp_path):
    import types

    queue = types.ModuleType("queue_under_test")
    exec(  # noqa: S102 - a module whose own clock calls the test swaps
         "import time\nimport datetime\nfrom datetime import datetime as dt\n"
         "from time import monotonic\n"
         "def stamp():\n    return time.time(), datetime.datetime.now(), dt.now(), monotonic()\n",
         queue.__dict__)
    real = queue.time

    @shape.scenario(max_restarts=0, expect_effects=False)
    def job(run):
        with shape.clock.patched(queue):
            run.advance(60)
            unix, module_now, class_now, mono = queue.stamp()
            note(run, unix=unix, module_now=module_now.isoformat(),
                 class_now=class_now.isoformat(), mono=mono)
        note(run, restored=queue.time is real)

    assert runner.execute(job, {"explicit": {}}, tmp_path).ok
    seen, restored = notes(tmp_path)
    epoch = shape.clock.EPOCH + datetime.timedelta(seconds=60)
    assert seen["unix"] == epoch.timestamp()
    assert seen["module_now"] == seen["class_now"] == epoch.replace(tzinfo=None).isoformat()
    assert seen["mono"] == 60
    assert restored == {"restored": True}
    assert abs(time.time() - epoch.timestamp()) > 1  # the real clock was never touched


# --------------------------------------------------------------------------------------- delivery


@shape.effect("notify.send", scope=lambda user: f"user:{user}", delivery="at-most-once")
def notify(user):
    pass


@shape.effect("notify.page", scope=lambda user: f"user:{user}")
def page(user):
    pass


def test_at_most_once_loss_is_by_design_and_duplicates_still_fail(tmp_path):
    @shape.scenario(max_restarts=0, invariants=[shape.exactly_once("notify.send")])
    def never_sent(run):
        with contextlib.suppress(Exception):  # fire and forget
            notify(1)

    server_error = FAULTS.index("server_error") + 1
    lost = runner.execute(never_sent, {"explicit": {"fault:notify.send#0.0": server_error}},
                          tmp_path / "lost")
    assert lost.ok, lost.violations
    assert lost.stats["by_design"] == {"notify.send": {"lost": True, "triggers": ["before_commit"]}}

    @shape.scenario(max_restarts=1, recovery="redeliver",
                    invariants=[shape.exactly_once("notify.send")])
    def resent(run):
        notify(1)

    crash_after = FAULTS.index("crash_after") + 1
    dup = runner.execute(resent, {"explicit": {"fault:notify.send#0.0": crash_after}}, tmp_path / "dup")
    assert dup.violated("exactly_once(notify.send)")


def test_a_lost_effect_without_the_declaration_is_still_a_defect(tmp_path):
    @shape.scenario(max_restarts=0, invariants=[shape.exactly_once("notify.page")])
    def never_paged(run):
        with contextlib.suppress(Exception):  # fire and forget
            page(1)

    server_error = FAULTS.index("server_error") + 1
    out = runner.execute(never_paged, {"explicit": {"fault:notify.page#0.0": server_error}}, tmp_path)
    assert out.violated("exactly_once(notify.page)")


def test_scenario_level_delivery_covers_ops_without_a_decorator(tmp_path):
    @shape.scenario(max_restarts=0, delivery={"notify.page": "at-most-once"},
                    invariants=[shape.exactly_once("notify.page")])
    def never_paged(run):
        with contextlib.suppress(Exception):  # fire and forget
            page(1)

    server_error = FAULTS.index("server_error") + 1
    out = runner.execute(never_paged, {"explicit": {"fault:notify.page#0.0": server_error}}, tmp_path)
    assert out.ok and out.stats["by_design"]["notify.page"]["lost"]


def test_by_design_section_in_the_coverage_report(tmp_path):
    @shape.effect("notify.keyed", scope=lambda user: f"user:{user}", key=lambda user: f"n-{user}",
                  delivery="at-most-once")
    def notify_keyed(user):
        pass

    @shape.scenario(max_restarts=2, recovery="redeliver",
                    invariants=[shape.exactly_once("notify.keyed")])
    def job(run):
        with contextlib.suppress(Exception):  # fire and forget
            notify_keyed(1)

    swept = runner.run_sweep(job, range(80), tmp_path)
    assert not swept.failing
    assert "by design" not in swept.cov.render()  # it is its own section, apart from coverage
    text = swept.cov.render_by_design()
    assert text.startswith("by design (declared at-most-once, a lost effect is not a defect;")
    [row] = swept.cov.by_design_report()
    assert row["op"] == "notify.keyed" and row["lost"] == len(row["lost_seeds"]) > 0
    assert row["seeds"] == 80 and set(row["triggers"]) <= {"before_commit", "reply_lost",
                                                          "crash_before", "crash_after"}
    assert f"notify.keyed: lost in {row['lost']} of 80 seeds (" in text
    assert f"e.g. seed {row['lost_seeds'][0]}" in text


def test_by_design_section_in_json_and_failing_reports(tmp_path, capsys):
    import json as jsonlib

    from shape import cli

    path = tmp_path / "checks.py"
    path.write_text("""
import shape

@shape.effect("sms.send", scope=lambda user: f"user:{user}", key=lambda user: f"sms-{user}",
              delivery="at-most-once")
def send(user):
    pass

@shape.effect("db.write", scope=lambda: "row")
def write():
    pass

@shape.scenario(max_restarts=1, recovery="redeliver",
                invariants=[shape.exactly_once("sms.send"), shape.at_most_once("db.write")])
def job(run):
    try:
        send("u1")
    except Exception:
        pass
    write()
    write()  # a real defect: written twice
""")
    code = cli.main(["test", str(path), "--seeds", "40", "--json", "--runs-dir", str(tmp_path / "r")])
    report = jsonlib.loads(capsys.readouterr().out)["scenarios"][0]
    assert code == 1  # the duplicate write fails; the lost texts do not
    [row] = report["by_design"]
    assert row["op"] == "sms.send" and row["lost"] > 0 and row["delivery"] == "at-most-once"
    assert {d["invariant"] for d in report["defects"]} == {"at_most_once(db.write)"}
    assert "by design" not in report["coverage"]


def test_delivery_values_are_checked():
    with pytest.raises(ValueError, match="delivery"):
        shape.effect("x", delivery="exactly-once")
    with pytest.raises(ValueError, match="delivery"):
        shape.scenario(lambda run: None, delivery={"x": "sometimes"})
