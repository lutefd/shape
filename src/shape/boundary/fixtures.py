"""Per-seed external resources: a database, a directory, a queue that each seed owns.

A fixture is a generator function that gets the seed's `shape.Run`, creates a resource, yields
its handle (a JSON value: a DSN, a path) and cleans up afterwards. The runner enters it once per
seed, in the process that runs that seed, so seeds running under `--jobs` never share a
resource. Incarnations (the processes that crash) see the handle as `run.fixture(name)`; the
resource itself survives their deaths and is torn down after the invariants have read it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time
import unittest
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

PG_ENV = "SHAPE_PG_DSN"


class FixtureUnavailable(unittest.SkipTest):
    """The resource a fixture needs is not there (no Postgres, no driver). pytest reports the
    test as skipped; `shape test` prints the reason and exits cleanly."""


@dataclass(frozen=True)
class Fixture:
    name: str
    setup: Callable[..., Iterator[Any]]
    available: Callable[[], str | None] | None = None

    def unavailable(self) -> str | None:
        """Why this fixture cannot run here, or None."""
        return self.available() if self.available is not None else None


def fixture(fn: Callable[..., Iterator[Any]] | None = None, *, name: str | None = None,
            available: Callable[[], str | None] | None = None) -> Any:
    """Declare a per-seed resource: `fn(run)` yields a JSON handle, then cleans up.

        @shape.fixture
        def jobs_db(run):
            dsn = make_database(f"jobs_{run.unique()}")
            yield dsn
            drop_database(dsn)

        @shape.scenario(fixtures=[jobs_db])
        def worker(run):
            connect(run.fixture("jobs_db"))
    """
    def wrap(f: Callable[..., Iterator[Any]]) -> Fixture:
        return Fixture(name or f.__name__, f, available)
    return wrap(fn) if fn is not None else wrap


def normalize(fixtures: Sequence[Fixture]) -> tuple[Fixture, ...]:
    seen: set[str] = set()
    for f in fixtures:
        if not isinstance(f, Fixture):
            raise TypeError(f"fixtures= takes @shape.fixture values; got {f!r}")
        if f.name in seen:
            raise ValueError(f"two fixtures are named {f.name!r}")
        seen.add(f.name)
    return tuple(fixtures)


def require(fixtures: Sequence[Fixture]) -> None:
    """Raise `FixtureUnavailable` before any seed runs if a fixture cannot be set up here."""
    for f in fixtures:
        if reason := f.unavailable():
            raise FixtureUnavailable(f"fixture {f.name!r} is unavailable: {reason}")


def unique_name(workdir: Path, prefix: str = "shape") -> str:
    """A resource name no other seed, worker process or run directory shares."""
    digest = hashlib.blake2b(f"{os.getpid()}:{workdir.resolve()}".encode(), digest_size=5)
    return f"{prefix}_{digest.hexdigest()}"


def enter(stack: contextlib.ExitStack, fixtures: Sequence[Fixture],
          make_run: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    """Set up each fixture in order and register its teardown on `stack`."""
    values: dict[str, Any] = {}
    for f in fixtures:
        generator = f.setup(make_run(dict(values)))
        try:
            value = next(generator)
        except StopIteration:
            raise RuntimeError(f"fixture {f.name!r} never yielded a value") from None
        stack.callback(_finish, f.name, generator)
        try:
            json.dumps(value)
        except TypeError:
            raise TypeError(f"fixture {f.name!r} yielded {value!r}; incarnations receive it as "
                            "JSON, so yield a string, number, list or dict (a DSN or a path)"
                            ) from None
        values[f.name] = value
    return values


def _finish(name: str, generator: Iterator[Any]) -> None:
    try:
        next(generator)
    except StopIteration:
        return
    raise RuntimeError(f"fixture {name!r} yielded more than once")


# ----------------------------------------------------------------------------------------- SQLite


def sqlite_copy(template: str | Path | None = None, *, init: Callable[[str], None] | None = None,
                name: str = "db") -> Fixture:
    """Give every seed its own copy of a SQLite database, in the seed's run directory.

    `template` is a database file with the schema (and any seed data) already in it, copied
    with SQLite's backup API so a WAL database copies consistently. Or pass `init(path)` to
    build the database from scratch for each seed. The fixture's value is the file path.
    """
    if (template is None) == (init is None):
        raise ValueError("sqlite_copy takes exactly one of template= and init=")

    def setup(run: Any) -> Iterator[str]:
        path = Path(run.dir) / f"{name}.sqlite3"
        if template is not None:
            with contextlib.closing(sqlite3.connect(template)) as src, \
                    contextlib.closing(sqlite3.connect(path)) as dst:
                src.backup(dst)
        else:
            assert init is not None
            init(str(path))
        yield str(path)
        # The file stays in a failing seed's kept run directory; the runner removes passing ones.

    return Fixture(name, setup)


# --------------------------------------------------------------------------------------- Postgres


def _driver() -> tuple[str, Any] | None:
    for module in ("psycopg", "psycopg2"):
        try:
            return module, __import__(module)
        except ImportError:
            continue
    return None


def postgres_dsn(dsn: str, dbname: str) -> str:
    """`dsn` with another database name, in the same URL or key/value form."""
    if "://" in dsn:
        parts = urlsplit(dsn)
        return urlunsplit(parts._replace(path="/" + dbname))
    if re.search(r"\bdbname\s*=", dsn):
        return re.sub(r"\bdbname\s*=\s*('[^']*'|\S+)", f"dbname={dbname}", dsn)
    return f"{dsn} dbname={dbname}"


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@contextlib.contextmanager
def _admin(dsn: str) -> Iterator[Any]:
    found = _driver()
    assert found is not None
    _, module = found
    conn = module.connect(dsn)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def postgres_unavailable(admin_dsn: str | None) -> str | None:
    """Why there is no usable Postgres for `postgres_clone`, or None when there is."""
    if _driver() is None:
        return "install psycopg (pip install 'psycopg[binary]') or psycopg2"
    if not admin_dsn:
        return f"set {PG_ENV} to a DSN with CREATEDB rights, or pass admin_dsn="
    try:
        with _admin(admin_dsn) as conn:
            conn.cursor().execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - any driver error means "not reachable here"
        return f"cannot connect to Postgres ({type(exc).__name__}: {exc})".splitlines()[0]
    return None


def postgres_clone(template: str, admin_dsn: str | None = None, *, name: str = "pg",
                   retries: int = 20) -> Fixture:
    """Give every seed its own Postgres database, cloned from a template database.

    `template` names a database that already has your schema (and seed rows). Each seed runs
    `CREATE DATABASE <unique> TEMPLATE <template>` through `admin_dsn` (default: the
    `SHAPE_PG_DSN` environment variable, a DSN with CREATEDB rights) and drops the clone when
    the seed is done, whether it passed or failed. The fixture's value is the clone's DSN, in
    the same form as `admin_dsn`. Nothing may be connected to the template while seeds run.
    Without a reachable Postgres or a driver the fixture is unavailable: pytest skips the test
    and `shape test` says why.
    """
    def resolved() -> str | None:
        return admin_dsn or os.environ.get(PG_ENV)

    def setup(run: Any) -> Iterator[str]:
        dsn = resolved()
        assert dsn is not None
        database = unique_name(Path(run.dir), "shape")
        with _admin(dsn) as conn:
            cur = conn.cursor()
            _drop(cur, database)  # a killed worker may have left this name behind
            for attempt in range(retries):
                try:
                    cur.execute(f"CREATE DATABASE {_quote(database)} TEMPLATE {_quote(template)}")
                    break
                except Exception as exc:
                    # Another seed may be cloning the same template at this very moment.
                    if "being accessed by other users" not in str(exc) or attempt == retries - 1:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        try:
            yield postgres_dsn(dsn, database)
        finally:
            with _admin(dsn) as conn:
                _drop(conn.cursor(), database)

    return Fixture(name, setup, lambda: postgres_unavailable(resolved()))


def _drop(cur: Any, database: str) -> None:
    cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (database,))
    cur.execute(f"DROP DATABASE IF EXISTS {_quote(database)}")
