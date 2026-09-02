"""APScheduler wiring for the two maintenance jobs (M8 step 10).

    decay       nightly     03:00
    reflection  weekly      Sunday 04:00

Four properties the plan asks for, and why each is not optional here:

`max_instances=1`     THE important one. Both jobs are long-running relative to
                      their schedule and both mutate rows across the table. Two
                      overlapping decay runs would each allocate their own
                      `decay_run_id`, so each would see the other's rows as
                      eligible and the "claimed by this run" stamp would stop
                      meaning anything — the SKIP LOCKED design assumes one run
                      id per sweep. APScheduler's default is `max_instances=1`
                      already; it is passed explicitly anyway, because a
                      default that happens to be right is not a decision, and
                      `test_scheduler_registers_both_cron_jobs` asserts on the
                      value rather than on the default.

`coalesce=True`       If the host was asleep over three fire times, run once on
                      wake, not three times. For an idempotent job (see
                      `jobs/decay.py`) three runs would be harmless but
                      pointless; for reflection they would produce three
                      summaries.

`misfire_grace_time`  How late a fire may be and still run. Generous — an hour
                      for decay, six for reflection — because these are
                      maintenance jobs with no user waiting: running the nightly
                      sweep at 03:40 after a restart is entirely fine, and
                      dropping it because the process came up forty minutes late
                      is not.

a persistent jobstore The plan says the store must survive restarts, so the
                      default `MemoryJobStore` will not do. `SQLAlchemyJobStore`
                      against the same Postgres keeps schedule state in an
                      `apscheduler_jobs` table.

WHY THE JOBSTORE URL NEEDS TRANSLATING
--------------------------------------
`DATABASE_URL` is `postgresql://...`, and SQLAlchemy 2.x resolves that scheme to
**psycopg2**, which is not installed — this project uses psycopg 3. So
`jobstore_url()` rewrites the scheme to `postgresql+psycopg://`, SQLAlchemy's
name for the psycopg-3 driver. Without it the scheduler dies at construction
with `ModuleNotFoundError: No module named 'psycopg2'`, which reads like a
missing dependency and is really a URL-dialect mismatch.

It uses the OWNER DSN, because the jobstore creates and migrates its own table.
That is schema work, which is the owner's job here in the same way migrations
are; the scheduler's own bookkeeping is not application data and is not
subject-scoped, so there is no RLS story to preserve for it.

WHY `build_scheduler()` IS NOT A SINGLETON
------------------------------------------
It returns a fresh scheduler each call so the unit test can build one with
`persistent=False` and inspect it without a database, a process, or a running
event loop. `main()` is the only caller that starts one.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from jobs.metrics import configure_logging, log_event

__all__ = [
    "DECAY_JOB_ID",
    "REFLECTION_JOB_ID",
    "JOB_IDS",
    "CRON",
    "jobstore_url",
    "build_scheduler",
    "run_forever",
]

DECAY_JOB_ID = "memsys-decay"
REFLECTION_JOB_ID = "memsys-reflection"

#: Exactly these two, and nothing else. The unit test compares the registered
#: job ids against this set for equality, not containment, so a third job added
#: without updating this constant is a test failure rather than a surprise in
#: production.
JOB_IDS: frozenset[str] = frozenset({DECAY_JOB_ID, REFLECTION_JOB_ID})

#: Cron expressions and the per-job scheduling policy, in one table.
CRON: dict[str, dict[str, Any]] = {
    DECAY_JOB_ID: {
        "trigger": {"hour": 3, "minute": 0},
        "misfire_grace_time": 3600,       # an hour late is still worth running
        "max_instances": 1,
        "coalesce": True,
    },
    REFLECTION_JOB_ID: {
        # "less frequent" per the plan: weekly rather than nightly. Consolidation
        # only has something to say once enough new raw memories have
        # accumulated to form a cluster, and it costs a completion per subject.
        "trigger": {"day_of_week": "sun", "hour": 4, "minute": 0},
        "misfire_grace_time": 21600,      # six hours
        "max_instances": 1,
        "coalesce": True,
    },
}


def jobstore_url() -> str:
    """The SQLAlchemy URL for the persistent jobstore. See the module docstring."""
    from store.db import admin_dsn, load_env

    load_env()
    explicit = os.environ.get("JOBS_SCHEDULER_URL")
    dsn = explicit or admin_dsn()
    parts = urlsplit(dsn)
    if parts.scheme in ("postgresql", "postgres"):
        parts = parts._replace(scheme="postgresql+psycopg")
    return urlunsplit(parts)


# ---------------------------------------------------------------------------
# the scheduled callables
# ---------------------------------------------------------------------------
#
# Module-level named functions, NOT lambdas or closures: `SQLAlchemyJobStore`
# pickles the job's callable by reference (`jobs.scheduler:scheduled_decay`), and
# a lambda cannot be referenced that way. A closure would raise at add_job time
# with "cannot be serialized" — which is exactly the failure a persistent
# jobstore is supposed to trade for.

async def scheduled_decay() -> None:
    """The nightly sweep. One worker; add more processes with `jobs.run`."""
    from jobs.decay import run_decay_worker

    await run_decay_worker(worker="scheduler")


async def scheduled_reflection() -> None:
    """The weekly consolidation, across every subject with enough candidates."""
    from jobs.reflection import run_reflection_worker

    await run_reflection_worker(worker="scheduler")


JOB_FUNCS = {
    DECAY_JOB_ID: scheduled_decay,
    REFLECTION_JOB_ID: scheduled_reflection,
}

JOB_NAMES = {
    DECAY_JOB_ID: "nightly memory decay",
    REFLECTION_JOB_ID: "weekly reflection / consolidation",
}


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------

def build_scheduler(*, persistent: bool = True, timezone: str = "UTC") -> Any:
    """An `AsyncIOScheduler` carrying exactly the two cron jobs. Not started.

    `persistent=False` swaps the SQLAlchemy jobstore for an in-memory one so the
    unit test can inspect the registration without a database. The job
    definitions — trigger, `max_instances`, misfire grace — are identical either
    way; only where the schedule is *stored* changes.

    An explicit `timezone` (UTC) rather than the host's: a cron job whose fire
    time moves twice a year with the operator's local DST is a maintenance
    surprise nobody wants, and APScheduler otherwise infers it from `tzlocal`.
    """
    from apscheduler.jobstores.memory import MemoryJobStore
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    if persistent:
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

        jobstores = {"default": SQLAlchemyJobStore(url=jobstore_url())}
    else:
        jobstores = {"default": MemoryJobStore()}

    scheduler = AsyncIOScheduler(jobstores=jobstores, timezone=timezone)

    for job_id, policy in CRON.items():
        scheduler.add_job(
            JOB_FUNCS[job_id],
            "cron",
            id=job_id,
            name=JOB_NAMES[job_id],
            max_instances=policy["max_instances"],
            coalesce=policy["coalesce"],
            misfire_grace_time=policy["misfire_grace_time"],
            replace_existing=True,
            **policy["trigger"],
        )

    return scheduler


async def run_forever(*, persistent: bool = True) -> None:  # pragma: no cover - a daemon
    """Start the scheduler and block. `python -m jobs.run --job scheduler`."""
    configure_logging()
    scheduler = build_scheduler(persistent=persistent)
    scheduler.start()
    for job in scheduler.get_jobs():
        log_event(
            "scheduler.job.registered",
            job_id=job.id,
            name=job.name,
            trigger=str(job.trigger),
            max_instances=job.max_instances,
            next_run=getattr(job, "next_run_time", None),
        )
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        scheduler.shutdown(wait=False)
