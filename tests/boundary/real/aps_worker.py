"""Run real APScheduler 3.x against a SQLAlchemy job store, under a virtual clock.

    python aps_worker.py crash DB T0 INTERVAL GRACE COALESCE
        schedule a job due at T0 (seconds since 2025-01-01 UTC) and die inside its first run
    python aps_worker.py restart DB T0 STEP...
        start a fresh scheduler on the same store with the clock at the first STEP, then move the
        clock to each later STEP, waking the scheduler each time. Prints what the job store ran.

INTERVAL is "date" for a one-shot job. GRACE is "none" for no misfire limit.
"""

import datetime as dt
import json
import os
import sys
import time

import apscheduler.executors.base as executors_base
import apscheduler.schedulers.base as schedulers_base
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

EPOCH = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
NOW = [EPOCH]


class VirtualDatetime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW[0].astimezone(tz) if tz else NOW[0].replace(tzinfo=None)


def at(seconds):
    return EPOCH + dt.timedelta(seconds=seconds)


def secs(moment):
    return (moment - EPOCH).total_seconds()


def dies():
    if os.environ.get("APS_DIE"):
        time.sleep(0.6)  # long enough for the scheduler to have saved the job's next run time
        os._exit(137)


def build(db):
    schedulers_base.datetime = executors_base.datetime = VirtualDatetime
    return BackgroundScheduler(jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{db}")},
                               timezone=dt.UTC)


def crash(db, t0, interval, grace, coalesce):
    os.environ["APS_DIE"] = "1"
    NOW[0] = at(float(t0))
    scheduler = build(db)
    options = {"id": "tick", "misfire_grace_time": None if grace == "none" else int(grace),
               "coalesce": coalesce == "true"}
    if interval == "date":
        scheduler.add_job("aps_worker:dies", "date", run_date=at(float(t0)), **options)
    else:
        scheduler.add_job("aps_worker:dies", "interval", seconds=float(interval),
                          start_date=at(float(t0)), next_run_time=at(float(t0)), **options)
    scheduler.start()
    time.sleep(10)  # the job kills this process first
    sys.exit(3)


def restart(db, steps):
    NOW[0] = at(steps[0])
    scheduler = build(db)
    seen = []

    def heard(event):
        kind = {EVENT_JOB_EXECUTED: "executed", EVENT_JOB_ERROR: "error",
                EVENT_JOB_MISSED: "missed"}[event.code]
        seen.append([kind, secs(event.scheduled_run_time), secs(NOW[0])])

    scheduler.add_listener(heard, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED)
    scheduler.start()
    time.sleep(0.4)
    for step in steps[1:]:
        NOW[0] = at(step)
        scheduler.wakeup()
        time.sleep(0.4)
    stored = [j.id for j in scheduler.get_jobs()]
    scheduler.shutdown(wait=False)
    print(json.dumps({"events": seen, "jobs": stored}))


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if sys.argv[1] == "crash":
        crash(*sys.argv[2:])
    else:
        restart(sys.argv[2], [float(x) for x in sys.argv[4:]])
