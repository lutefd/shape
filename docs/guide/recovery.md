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

**`none`.** A standalone handler or a service with automatic restart disabled gets one attempt. If it dies, the run reports a termination failure and still evaluates the logged effects. This matches a `systemd` service with `Restart=no` and no external redelivery. [systemd service restart policy](https://man7.org/linux/man-pages/man5/systemd.service.5.html#OPTIONS)

**`redeliver`.** An at-least-once queue or supervisor sends the same job to a new worker after a failed attempt. Shape reruns the scenario with the same job input and durable state. With the OpenAI Agents SDK driver, an explicitly saved approval state can be picked up on redelivery. [RabbitMQ consumer requeueing](https://www.rabbitmq.com/docs/confirms#automatic-requeueing)

**`langgraph-resume`.** A replacement worker opens the same durable checkpointer and resumes the same `thread_id`. `shape.langgraph.drive` and `adrive` send `None` as the input on restart so LangGraph loads the last durable checkpoint and reexecutes unfinished graph work. Keep the checkpointer data in `run.dir` and supply the same thread configuration on every incarnation. [LangGraph checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)

**`cron-retick`.** The next scheduled tick starts a fresh invocation of the same job. Shape uses the scenario entry point and the same durable storage on the next incarnation. Choose the input in the scenario that the next real tick would see. The preset advances directly to that invocation. [Kubernetes CronJob scheduling](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/#cronjob-limitations)

**`apscheduler-reload`.** A scheduler starts against a persistent job store and deserializes stored jobs. Shape models one eligible stored job running after startup. Configure the scenario's job function and arguments to match that stored job. APScheduler's misfire grace time and coalescing settings determine eligibility in production. [APScheduler persistent jobs and misfires](https://apscheduler.readthedocs.io/en/3.x/userguide.html#missed-job-executions-and-coalescing)

**`celery-acks-late`.** The broker can redeliver an unacknowledged task when the worker dies, so Shape runs the same task input after a process crash or kill. For a dead pool child with its parent worker alive, Celery needs `task_reject_on_worker_lost=True` as well as late acknowledgement to model this behavior. A task exception ends the attempt without harness redelivery. [Celery worker-loss configuration](https://docs.celeryq.dev/en/stable/userguide/configuration.html#task-reject-on-worker-lost)

Omitting `recovery=` retains the normal runner behavior and leaves any adapter `on_restart=` policy in control of that adapter. Use one of these two configuration points per scenario. With `recovery=` set, the runner emits the preset name in the restarted incarnation's trace, and the adapters use the corresponding restart input. For a deployment with additional scheduling rules, put that logic in the scenario and use `run.restarts` to detect a restarted incarnation.
