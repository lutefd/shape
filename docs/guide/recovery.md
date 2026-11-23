# Recovery presets

Set `recovery=` on `@shape.scenario` to model how production starts work after an incarnation fails. One incarnation runs per attempt. Each new attempt shares `run.dir` and increments `run.restarts`, up to `max_restarts`. Point the scenario at the same durable business job each time.

```python
@shape.scenario(invariants=CHARGE_ONCE, max_restarts=3, recovery="cron-retick")
def async_per_attempt_key(run: shape.Run) -> None:
    asyncio.run(handle(run.dir, "o1", charge_per_attempt_key))
```

In the [runnable asyncio example](examples/async_agent.py), seed 13 lands the charge, dies, and starts the same job from its entry point on the next tick. The trace identifies the action supplied by the harness.

```sh
shape test docs/guide/examples/async_agent.py::async_per_attempt_key --start 13 --seeds 1
```

```text
    incarnation 0
      1. Payments.charge(order_id='o1', amount=500, idempotency_key='o1-0')
           effect landed (idempotency key 'o1-0'), then process killed after the effect landed, before the caller saw it
      -> process died
    incarnation 1 (restarted)
      harness: cron re-ticked the job (recovery='cron-retick')
      2. Payments.charge(order_id='o1', amount=500, idempotency_key='o1-0')
           timeout before the effect: the caller saw TimeoutError
      3. Payments.charge(order_id='o1', amount=500, idempotency_key='o1-1')
           effect landed (idempotency key 'o1-1')
      finished
```

## Presets

Each preset decides three things when an incarnation dies: whether the deployment starts the job again, how much virtual time passes first (see [world fixtures](world-fixtures.md#time-between-incarnations)), and which scheduled ticks the scenario is delivered. A preset that names a real system enforces that system's rule instead of only rerunning the scenario.

| Preset | Restarts after | Time passes | Checked against |
| --- | --- | --- | --- |
| `none` | never | | n/a |
| `redeliver` | any failed attempt | none | documented, not run: [RabbitMQ requeueing](https://www.rabbitmq.com/docs/confirms#automatic-requeueing) |
| `langgraph-resume` | any failed attempt | none | the LangGraph driver tests, with a real checkpointer |
| `cron-retick` | any failed attempt | `every` seconds (default 0) | documented, not run: [Kubernetes CronJob](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/#cronjob-limitations) |
| `apscheduler-reload` | a crash, for a recurring job only | until the next due tick | **verified** against APScheduler 3.11, real library, real crash |
| `celery-acks-late` | a crash, only with `reject_on_worker_lost` or a lost worker | none, or `visibility_timeout` on Redis | **verified** against Celery 5.7, real worker, real `SIGKILL` |
| `lease-takeover` | a crash or hang | the lease plus the reaper interval | **unverified**: a common pattern, no library to run it against |

Pass the name for the defaults, or a policy object from `shape.recovery` to set the system's own configuration:

```python
@shape.scenario(recovery="lease-takeover")
@shape.scenario(recovery=shape.recovery.LeaseTakeover(lease_seconds=120, reclaim_interval=5))
@shape.scenario(recovery=shape.recovery.APSchedulerReload(interval=3600, misfire_grace_time=300))
@shape.scenario(recovery=shape.recovery.CeleryAcksLate(reject_on_worker_lost=True))
```

**`none`.** A standalone handler or a service with automatic restart disabled gets one attempt. If it dies, the run reports a termination failure and still evaluates the logged effects. This matches a `systemd` service with `Restart=no` and no external redelivery. [systemd service restart policy](https://man7.org/linux/man-pages/man5/systemd.service.5.html#OPTIONS)

**`redeliver`.** An at-least-once queue or supervisor sends the same job to a new worker after a failed attempt. Shape reruns the scenario with the same job input and durable state. With the OpenAI Agents SDK driver, an explicitly saved approval state can be picked up on redelivery. `shape.recovery.Redeliver(delay=30)` waits before redelivering.

**`langgraph-resume`.** A replacement worker opens the same durable checkpointer and resumes the same `thread_id`. `shape.langgraph.drive` and `adrive` send `None` as the input on restart so LangGraph loads the last durable checkpoint and reexecutes unfinished graph work. Keep the checkpointer data in `run.dir` and supply the same thread configuration on every incarnation. [LangGraph checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)

**`cron-retick`.** The next scheduled tick starts a fresh invocation of the same job. Shape uses the scenario entry point and the same durable storage on the next incarnation. Choose the input in the scenario that the next real tick would see. `shape.recovery.CronRetick(every=300)` makes five virtual minutes pass first, so code that reads `shape.clock.now()` sees the next tick's time.

**`apscheduler-reload`.** APScheduler 3.x with a persistent job store. The scenario is the job's body, started once per delivered tick. The scheduler moves a job's `next_run_time` forward when it submits the job, so the tick that crashed is **not** rerun after the restart:

- A one-shot (`date`) job is removed from the store when it is submitted. A crash loses it for good, and the run reports that the scenario did not finish. That is the default, because the preset cannot know a period.
- A recurring job needs `APSchedulerReload(interval=seconds)`. After the restart the scheduler delivers every tick that came due while the process was down, only the latest if `coalesce` (APScheduler's default), and drops those later than `misfire_grace_time` (default 1 second; `None` never drops). If nothing is left it waits for the next tick, and the trace shows the time that passes.
- `downtime=seconds` is how long the process stays down. A job that raised (the process lived) simply waits for its next tick.

The preset does not model `jitter`, `max_instances`, or code that re-adds the job on boot with `replace_existing=True`; put that scheduling in the scenario. [APScheduler persistent jobs and misfires](https://apscheduler.readthedocs.io/en/3.x/userguide.html#missed-job-executions-and-coalescing)

**`celery-acks-late`.** `acks_late=True`, with the rest of Celery's defaults. Shape kills the task's process, which is a pool child with its parent worker alive, so Celery acknowledges the task when it sees the death and the work is lost unless `task_reject_on_worker_lost=True`:

- `CeleryAcksLate()` (the name alone): a crash is not redelivered.
- `CeleryAcksLate(reject_on_worker_lost=True)`: the message is requeued and the task runs again. Celery 5.7 requeues it every time, with no cap.
- `CeleryAcksLate(lost="worker")`: the whole worker died (a killed container), so the broker returns the unacknowledged message: at once on RabbitMQ, after `visibility_timeout` (default 3600 seconds, advanced on the virtual clock) with `broker="redis"`. This case is not run against a broker.
- A task that raised is acknowledged and never redelivered (`task_acks_on_failure_or_timeout`). A hang that Celery's time limit kills is acknowledged too, which the preset applies but the verification tests do not exercise.

[Celery worker-loss configuration](https://docs.celeryq.dev/en/stable/userguide/configuration.html#task-reject-on-worker-lost)

**`lease-takeover`.** A job queue where a worker holds a lease (a Postgres row with `lease_until`, an SQS visibility timeout, a Redis lock). A dead worker cannot renew or release it, so another worker reclaims the job only after `lease_seconds` (default 30) plus a reaper interval (default 1 second). Until the clock gets there, nothing runs. Compare against `shape.clock.now()`, not the database's `now()`, and read which worker you are from `run.worker` (`worker-0`, then `worker-1`). A job that raised is the live worker's to fail or retry, so it is not a takeover. This preset is **unverified**: it models a pattern, and there is no library whose behaviour it can be run against. The [worked example](world-fixtures.md#a-leased-job-queue-end-to-end) shows it finding a double run.

### How the verified presets were checked

```sh
pip install 'apscheduler<4' sqlalchemy celery
pytest tests/boundary/test_preset_apscheduler.py tests/boundary/test_preset_celery.py
```

The APScheduler tests start a real scheduler on a SQLAlchemy job store, kill its process inside a job, start a second scheduler on the same store at a virtual time, and compare what it runs with what the preset predicts for eight combinations of interval, downtime, `misfire_grace_time` and `coalesce`. The Celery tests run a real prefork worker (in-memory broker), `SIGKILL` the task's process, and compare how many times the task starts with the preset. Both modules skip when the library is not installed.

Omitting `recovery=` retains the normal runner behavior (a restart after every death, no time passing) and leaves any adapter `on_restart=` policy in control of that adapter. Use one of these two configuration points per scenario. With `recovery=` set, the runner emits the preset name in the restarted incarnation's trace, and the adapters use the corresponding restart input. For a deployment with additional scheduling rules, put that logic in the scenario and use `run.restarts` to detect a restarted incarnation, or subclass `shape.recovery.Policy`.
