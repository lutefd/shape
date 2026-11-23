# World fixtures: setup, per-seed resources, time and lossy effects

An agent system that matters runs against a world: a database with rows in it, a queue with jobs, a clock that leases and retries depend on. These pieces let a scenario build that world for every seed without giving up determinism or parallelism.

| You need | Use |
| --- | --- |
| Seed rows, enqueue a job, create a customer before the agent starts, with no faults | [`setup=`](#fault-free-setup) and [`initial_state=`](#seeding-a-fake) |
| A database, queue or directory per seed, never shared across `--jobs` | [`@shape.fixture`](#per-seed-resources), `shape.fixtures.sqlite_copy`, `shape.fixtures.postgres_clone` |
| A lease to expire, a retry delay to elapse, a tick to come due | [`shape.clock`, `run.advance`, recovery presets](#time-between-incarnations) |
| An effect that may be lost by design, such as a text message | [`delivery="at-most-once"`](#effects-that-are-lossy-by-design) |

A [worked example](#a-leased-job-queue-end-to-end) uses all four.

## Fault-free setup

`@shape.scenario(setup=fn)` runs `fn(run)` once per seed, in the parent, before the first incarnation starts. During it no fault is injected, nothing crashes, and `shape.choose` returns its nominal option. Effects and network calls made there are logged as setup: the trace shows them under `setup (no faults)`, and no invariant counts them unless it asks.

```python
@shape.effect("queue.enqueue", scope=lambda job_id, **_: f"job:{job_id}", hide=["path"])
def enqueue_job(path, job_id): ...

def enqueue(run: shape.Run) -> None:
    enqueue_job(run.fixture("queue"), "j1")

@shape.scenario(fixtures=[QUEUE], setup=enqueue, ...)
```

A custom invariant reaches setup through the `World`:

```python
world.effects("queue.enqueue")                      # [] : setup is not counted
world.effects("queue.enqueue", include_setup=True)  # the setup calls
world.setup                                         # every entry the setup phase logged
```

If `setup` raises, the seed reports a `harness_error` naming your function and no incarnation starts. The setup is part of what a replay re-runs, so it must be deterministic. A scenario whose only effects are setup effects still reports `NOTHING TESTED`.

### Seeding a fake

Every provider fake accepts `initial_state=`: a JSON dict merged into the fake's state before the first call of every seed. Its keys are the fake's own state keys, the same JSON it keeps between calls (see the fake's source). A restarted incarnation sees what the agent changed, not the seed again.

```python
SlackFake(initial_state={"channels": {"C1": []}})
```

`LangGraphFake` also takes `threads=[...]`, dicts with an optional `thread_id` (a UUID; one is derived from the position otherwise), `metadata`, `values` and `status`, so a scenario can resume a conversation that already exists:

```python
fake = shape.fakes.LangGraphFake(threads=[{"metadata": {"user": "u1"}, "values": {"messages": []}}])
```

## Per-seed resources

A fixture is a generator that gets the seed's `shape.Run`, creates a resource, yields a JSON handle (a DSN or a path) and cleans up. The runner enters it once per seed, in the process that runs the seed, before `setup`, so seeds running under `--jobs` never share a resource. Incarnations read the handle with `run.fixture(name)`. The resource outlives the processes that crash, and it is torn down after the invariants have read it (`world.run.fixture(name)` works in an invariant), whether the seed passed or failed.

```python
@shape.fixture
def scratch_bucket(run):
    name = run.unique("bucket")      # unique per seed and per --jobs worker
    create_bucket(name)
    yield name
    delete_bucket(name)

@shape.scenario(fixtures=[scratch_bucket])
def job(run):
    upload(run.fixture("scratch_bucket"), ...)
```

Fixtures are set up in the order listed, and a later one can read an earlier one with `run.fixture`. A fixture that raises is a `harness_error`, and the ones already set up are still torn down. A value that is not JSON is rejected, because incarnations started with `spawn` receive it as JSON.

### SQLite

`shape.fixtures.sqlite_copy(template)` copies a template database file with SQLite's backup API (a WAL database copies consistently) into the seed's run directory. `sqlite_copy(init=build)` calls `build(path)` to make a fresh one instead. A failing seed keeps its run directory, database file included, so you can open it afterwards.

```python
QUEUE = shape.fixtures.sqlite_copy("tests/queue-template.sqlite3", name="queue")
```

### Postgres

`shape.fixtures.postgres_clone(template)` runs `CREATE DATABASE <unique> TEMPLATE <template>` for every seed and drops the clone afterwards, pass or fail. The value is the clone's DSN, in the same form as the admin DSN. It needs `pip install 'psycopg[binary]'` (or psycopg2) and a role with `CREATEDB`, read from `SHAPE_PG_DSN`:

```sh
docker run -d --rm --name shape-pg -e POSTGRES_PASSWORD=shape -p 127.0.0.1:5432:5432 postgres:17
export SHAPE_PG_DSN=postgresql://postgres:shape@127.0.0.1:5432/postgres
python docs/guide/examples/postgres_queue.py     # runs your migrations once into a template database
shape test docs/guide/examples/postgres_queue.py::pg_lease_keyed --seeds 100 --jobs 4
```

Build the template once with your real migrations; nothing may stay connected to it while seeds run. Postgres copies a template by file, so a clone takes milliseconds, and clones created by `--jobs` workers at the same moment do not collide: each name is derived from the process and the seed's run directory. A clone left behind by a killed worker is dropped the next time its name is used.

When Postgres or the driver is missing, the fixture is *unavailable* rather than broken. `shape test` prints `SKIP <scenario>: fixture 'pg' is unavailable: <reason>` and exits 0 for that scenario, and `shape.check` raises `shape.FixtureUnavailable`, which pytest reports as a skipped test. In CI, give the job a Postgres service and set `SHAPE_PG_DSN`:

```yaml
services:
  postgres:
    image: postgres:17
    env: { POSTGRES_PASSWORD: shape }
    ports: ["5432:5432"]
    options: >-
      --health-cmd "pg_isready -U postgres" --health-interval 5s --health-retries 10
env:
  SHAPE_PG_DSN: postgresql://postgres:shape@127.0.0.1:5432/postgres
```

For another database (MySQL, Redis, S3 in MinIO, Kafka topics), write a `@shape.fixture` with the same shape: create under `run.unique(...)`, yield the handle, drop it. `shape.fixtures.Fixture(name, setup, available)` also takes an `available()` callable that returns a reason string, so the same skip applies.

## Time between incarnations

Leases expire, retries wait, ticks come due, and none of that should take real seconds or touch the system clock. A run has a virtual clock that starts at `shape.clock.EPOCH` (2025-01-01 UTC) and moves only when something advances it:

- **The runner, on a restart.** A recovery preset says how long the deployment takes to bring the work back ([`lease-takeover`](recovery.md#presets) waits out the lease, `apscheduler-reload` waits for the next tick). `@shape.scenario(advance_on_restart=300)` adds a fixed delay before every restart on top of that.
- **A world actor or the scenario.** `run.advance(seconds)` and `shape.clock.advance(seconds)` let time pass; `shape.clock.sleep(seconds)` advances instead of waiting.

Code under test opts in by asking the clock. Outside a shape run the same calls read the real clock, so production behaviour is unchanged:

```python
now = shape.clock.now()          # timezone-aware UTC datetime
shape.clock.time()               # seconds since the Unix epoch
shape.clock.monotonic()          # virtual seconds since the run started
run.now()                        # the same reading from a Run (also in an invariant)
```

Pass the time into your own queries instead of calling the database's `NOW()`, which shape does not control: `WHERE lease_until < %s` with `shape.clock.now()` as the parameter. The trace shows where time passed: `time passed: 31s (clock 2025-01-01T00:00:31Z)`.

### Patching a module's clock

Code you cannot edit calls `time.time()` or `datetime.now()`. `shape.clock.patched(module, ...)` points those modules' own references at the virtual clock for the length of a `with` block. It covers `import time`, `import datetime`, `from datetime import datetime` and `from time import time`, `monotonic`, `sleep` or `perf_counter` in the named modules' globals, and nothing else: the standard library, other modules and the real clock stay untouched, and boundary mode never seals a real clock globally.

```python
import my_queue, my_worker

@shape.scenario(recovery="lease-takeover")
def worker(run):
    with shape.clock.patched(my_queue, my_worker):   # patch inside the scenario: every incarnation is its own process
        my_worker.run_once()
```

Two limits: a module that imported the clock function under another name (`from time import time as now_ts`) is patched too, but a clock read inside a C extension or in SQL is not. For those, pass the time in explicitly.

## Effects that are lossy by design

A text message nobody retries, a best-effort metric, a notification that may drop. `@shape.effect(..., delivery="at-most-once")` declares that losing it is the design. For `shape.exactly_once(op)`:

- A **lost** effect is not a defect. The coverage report lists it under its own heading with how often it happened, so a rate that grows is still visible.
- A **duplicate** still fails, as it would for any effect.

For ops that come from a fake or a framework adapter, declare it on the scenario instead: `@shape.scenario(delivery={"slack.chat.postMessage": "at-most-once"})`. The scenario's map wins over the effect's.

```python
@shape.effect("sms.send", scope=lambda user: f"user:{user}", delivery="at-most-once")
def send_sms(user): ...
```

Without an idempotency key, redelivery after a crash sends it twice, and that fails:

```sh
shape test docs/guide/examples/world_fixtures.py::sms_unkeyed --seeds 100
```

```text
coverage over 100 seeds:
  op         landed  attempts
  sms.send       88       108
            faults injected: client_error 4, connection_reset_after_commit 2, crash_after 2, crash_before 6, server_error 8, timeout 2, timeout_after_commit 4
  model choices taken: none
  interleaving choices taken: none
by design (declared at-most-once, a lost effect is not a defect; a duplicate still fails):
  sms.send: lost in 14 of 100 seeds
scope assignment:
  sms.send: explicit scope= (e.g. 'user:u1')
1 defect across 2 failing seeds

defect 1 of 1: exactly_once(sms.send)  op=sms.send  scope=user:<id>  (2 of 2 failing seeds)
  crash after commit: 2 seeds (crash_after)
FAIL docs/guide/examples/world_fixtures.py::sms_unkeyed  seed=31  invariant=exactly_once(sms.send)
  sms.send happened 2 times for scope 'user:u1' (it must happen exactly once)
  minimized to 2 steps (1 of 1 deviations kept, 2 replays):
    incarnation 0
      1. send_sms(user='u1')
           effect landed, then process killed after the effect landed, before the caller saw it
      -> process died
    incarnation 1 (restarted)
      harness: redelivered the request (recovery='redeliver')
      2. send_sms(user='u1')
           effect landed
      finished
  replay: runs/sms_unkeyed/seed-31.min.json
```

With a key the provider dedupes the repeat. The same seeds pass, and the loss stays on the report:

```sh
shape test docs/guide/examples/world_fixtures.py::sms_keyed --seeds 100
```

```text
coverage over 100 seeds:
  op         landed  attempts
  sms.send       86       108
            faults injected: client_error 4, connection_reset_after_commit 2, crash_after 2, crash_before 6, server_error 8, timeout 2, timeout_after_commit 4
  model choices taken: none
  interleaving choices taken: none
by design (declared at-most-once, a lost effect is not a defect; a duplicate still fails):
  sms.send: lost in 14 of 100 seeds
scope assignment:
  sms.send: explicit scope= (e.g. 'user:u1')
PASS
```

## A leased job queue, end to end

[`world_fixtures.py`](examples/world_fixtures.py) models a queue where a worker claims a job with a lease. The queue is a SQLite file every seed copies (`sqlite_copy`), a setup step enqueues the job, and `lease-takeover` lets a second worker reclaim it once the lease runs out. The claim query compares `lease_until` with `shape.clock.now()`. `lease_unkeyed` runs the agent step with no idempotency key:

```sh
shape test docs/guide/examples/world_fixtures.py::lease_unkeyed --start 39 --seeds 1
```

```text
scenario=lease_unkeyed  seeds=1 (from 39)  failing=1  wall=0.29s  3 seeds/s
coverage over 1 seeds:
  op          landed  attempts
  agent.run        2         3
             faults injected: crash_after 1, server_error 1
  model choices taken: none
  interleaving choices taken: none
scope assignment:
  agent.run: explicit scope= (e.g. 'job:j1')
1 defect across 1 failing seeds

defect 1 of 1: exactly_once(agent.run)  op=agent.run  scope=job:<id>  (1 of 1 failing seeds)
  before-commit fault: 1 seed (server_error)
  crash after commit: 1 seed (crash_after)
FAIL docs/guide/examples/world_fixtures.py::lease_unkeyed  seed=39  invariant=exactly_once(agent.run)
  agent.run happened 2 times for scope 'job:j1' (it must happen exactly once)
  minimized to 3 steps (2 of 2 deviations kept, 4 replays):
    setup (no faults)
      enqueue_job(job_id='j1')
    incarnation 0
      1. run_agent(job_id='j1')
           server error before the effect: the caller saw ServerError
      2. run_agent(job_id='j1')
           effect landed, then process killed after the effect landed, before the caller saw it
      -> process died
    incarnation 1 (restarted)
      time passed: 31s (clock 2025-01-01T00:00:31Z)
      harness: another worker reclaimed the job after its lease expired (recovery='lease-takeover')
      3. run_agent(job_id='j1')
           effect landed
      finished
  replay: runs/lease_unkeyed/seed-39.min.json
```

The first worker's run landed and then the process died before it could mark the job done. Its lease held for 30 seconds, so nothing could take the job over until the clock passed it; the trace shows 31 seconds going by (the 30 second lease plus the reaper's 1 second poll), and the second worker ran the step again. With `key=lambda job_id: f"run-{job_id}"` the provider dedupes the repeat and the same scenario passes (`lease_keyed`, 40 seeds):

```text
scenario=lease_keyed  seeds=40 (from 0)  failing=0  wall=3.04s  13 seeds/s
...
PASS
```

On Postgres the scenario is the same with `postgres_clone` in place of `sqlite_copy` ([`postgres_queue.py`](examples/postgres_queue.py)). This is the real output against Postgres 17, with the clones created by four `--jobs` workers:

```sh
shape test docs/guide/examples/postgres_queue.py::pg_lease_unkeyed --start 39 --seeds 1
```

```text
FAIL docs/guide/examples/postgres_queue.py::pg_lease_unkeyed  seed=39  invariant=exactly_once(agent.run)
  agent.run happened 2 times for scope 'job:j1' (it must happen exactly once)
  minimized to 3 steps (2 of 2 deviations kept, 4 replays):
    setup (no faults)
      enqueue_job(job_id='j1')
    incarnation 0
      1. run_agent(job_id='j1')
           server error before the effect: the caller saw ServerError
      2. run_agent(job_id='j1')
           effect landed, then process killed after the effect landed, before the caller saw it
      -> process died
    incarnation 1 (restarted)
      time passed: 31s (clock 2025-01-01T00:00:31Z)
      harness: another worker reclaimed the job after its lease expired (recovery='lease-takeover')
      3. run_agent(job_id='j1')
           effect landed
      finished
  replay: runs/pg_lease_unkeyed/seed-39.min.json
```

```sh
shape test docs/guide/examples/postgres_queue.py::pg_lease_keyed --seeds 100 --jobs 4
```

```text
scenario=pg_lease_keyed  seeds=100 (from 0)  failing=0  wall=6.12s  16 seeds/s
...
PASS
```

### A scheduler tick that is not rerun

The same pieces test an APScheduler job. `digest_current_hour` sends the digest for the hour its tick started in, with `recovery=APSchedulerReload(interval=3600)`. When a tick fails, APScheduler has already moved the job's next run forward, so the next run is the next hour and the missed hour is never sent:

```sh
shape test docs/guide/examples/world_fixtures.py::digest_current_hour --start 2 --seeds 1
```

```text
FAIL docs/guide/examples/world_fixtures.py::digest_current_hour  seed=2  invariant=every_hour_has_a_digest
  no digest was sent for hour 0
  minimized to 2 steps (1 of 1 deviations kept, 2 replays):
    incarnation 0
      1. send_digest(hour=0)
           timeout before the effect: the caller saw TimeoutError
      process raised: TimeoutError: shape: injected timeout
    incarnation 1 (restarted)
      time passed: 3600s (clock 2025-01-01T01:00:00Z)
      harness: APScheduler reloaded the job store; the crashed tick was not rerun (recovery='apscheduler-reload')
      harness: tick scheduled for 2025-01-01T01:00:00Z
      2. send_digest(hour=1)
           effect landed (idempotency key 'digest-1')
      finished
  replay: runs/digest_current_hour/seed-2.min.json
```

`digest_catches_up` records the last hour it sent and sends every hour since, so a lost tick is made up at the next one. It passes all 100 seeds.
