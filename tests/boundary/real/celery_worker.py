"""Run one task on a real Celery worker (prefork pool, in-memory broker, acks_late) and print how
many times it started.

    python celery_worker.py reject|default kill|raise DEATHS EXPECTED
"""

import os
import signal
import sys
import tempfile
import time

from celery import Celery
from celery.contrib.testing.worker import start_worker

mode, how, deaths, expected = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
log = os.path.join(tempfile.mkdtemp(), "log")

app = Celery("shape_preset_check", broker="memory://", backend="cache+memory://")
app.conf.task_acks_late = True
app.conf.task_reject_on_worker_lost = mode == "reject"
app.conf.worker_prefetch_multiplier = 1


@app.task(name="work")
def work():
    with open(log, "a") as f:
        f.write("start\n")
    with open(log) as f:
        starts = f.read().count("start")
    if starts <= deaths:
        if how == "kill":
            os.kill(os.getpid(), signal.SIGKILL)
        raise RuntimeError("the task failed")


def starts():
    return open(log).read().count("start") if os.path.exists(log) else 0


with start_worker(app, pool="prefork", concurrency=1, perform_ping_check=False, loglevel="CRITICAL"):
    result = work.delay()
    deadline = time.monotonic() + 60
    # Celery notices a dead pool child only after about ten seconds. The task is settled once the
    # worker records its failure (acknowledged) or it starts the expected number of times.
    while not result.ready() and starts() < expected and time.monotonic() < deadline:
        time.sleep(0.2)
    time.sleep(2.0)  # a task that must not be redelivered gets a chance to prove it
    print(starts(), flush=True)
    os._exit(0)  # the pool is still replacing its dead child; do not wait for a clean shutdown
