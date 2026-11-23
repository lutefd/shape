"""`shape.fixtures.postgres_clone` against a real Postgres. Set SHAPE_PG_DSN to a DSN with CREATEDB
rights (see docs/guide/world-fixtures.md for a Docker one-liner); without one these tests skip."""

import os

import pytest

import shape
from shape.boundary import fixtures

DSN = os.environ.get("SHAPE_PG_DSN")
TEMPLATE = "shape_pytest_template"

needs_postgres = pytest.mark.skipif(
    bool(fixtures.postgres_unavailable(DSN)),
    reason=fixtures.postgres_unavailable(DSN) or "")


def connect(dsn):
    _, driver = fixtures._driver()
    conn = driver.connect(dsn)
    conn.autocommit = True
    return conn


@pytest.fixture
def template():
    with connect(DSN) as admin:
        cur = admin.cursor()
        fixtures._drop(cur, TEMPLATE)
        cur.execute(f'CREATE DATABASE "{TEMPLATE}"')
    with connect(fixtures.postgres_dsn(DSN, TEMPLATE)) as conn:
        conn.cursor().execute("CREATE TABLE jobs (id integer PRIMARY KEY, state text)")
        conn.cursor().execute("INSERT INTO jobs VALUES (0, 'seeded')")
    yield TEMPLATE
    with connect(DSN) as admin:
        fixtures._drop(admin.cursor(), TEMPLATE)


@shape.effect("jobs.write", scope=lambda: "job")
def write():
    pass


@shape.invariant
def own_database_only(world):
    with connect(world.run.fixture("pg")) as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM jobs")
        rows = cur.fetchone()[0]
    return None if rows == 2 else f"{rows} rows: seeds shared a database"


def run_job(run):
    with connect(run.fixture("pg")) as conn:
        conn.cursor().execute("INSERT INTO jobs VALUES (%s, 'done') ON CONFLICT (id) DO NOTHING",
                              (run.seed + 1,))
    write()


@needs_postgres
def test_every_seed_gets_its_own_clone_and_drops_it(template, tmp_path):
    scenario = shape.scenario(run_job, fixtures=[shape.fixtures.postgres_clone(template)],
                              invariants=[own_database_only])
    shape.check(scenario, seeds=16, jobs=4, runs_dir=tmp_path)
    with connect(DSN) as admin:
        cur = admin.cursor()
        cur.execute("SELECT count(*) FROM pg_database WHERE datname ~ '^shape_[0-9a-f]{10}$'")
        assert cur.fetchone()[0] == 0, "a clone outlived its seed"


@needs_postgres
def test_clone_is_dropped_even_when_the_seed_fails(template, tmp_path):
    from shape.boundary import runner

    @shape.scenario(fixtures=[shape.fixtures.postgres_clone(template)],
                    invariants=[shape.invariant(lambda world: "always fails")])
    def failing(run):
        write()

    out = runner.execute(failing, {"explicit": {}}, tmp_path)
    assert out.violations
    with connect(DSN) as admin:
        cur = admin.cursor()
        cur.execute("SELECT count(*) FROM pg_database WHERE datname ~ '^shape_[0-9a-f]{10}$'")
        assert cur.fetchone()[0] == 0


def test_without_a_reachable_postgres_the_fixture_is_unavailable(tmp_path):
    clone = shape.fixtures.postgres_clone("template", "postgresql://nobody@127.0.0.1:1/postgres")
    scenario = shape.scenario(lambda run: write(), fixtures=[clone])
    with pytest.raises(shape.FixtureUnavailable):  # pytest reports this as a skip
        shape.check(scenario, seeds=1, runs_dir=tmp_path)


def test_dsn_forms_keep_everything_but_the_database_name():
    assert fixtures.postgres_dsn("postgresql://u:p@h:5/postgres?sslmode=disable", "x") == (
        "postgresql://u:p@h:5/x?sslmode=disable")
    assert fixtures.postgres_dsn("host=h user=u dbname=postgres", "x") == "host=h user=u dbname=x"
    assert fixtures.postgres_dsn("host=h user=u", "x") == "host=h user=u dbname=x"
