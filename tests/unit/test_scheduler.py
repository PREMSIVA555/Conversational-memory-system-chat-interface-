"""Unit test for the APScheduler wiring (M8 step 10).

No database: `build_scheduler(persistent=False)` swaps the SQLAlchemy jobstore
for an in-memory one, and the job definitions — trigger, `max_instances`,
misfire grace, callable — are identical either way. The one thing that in-memory
substitution cannot check is that the persistent store is configured correctly,
so that is asserted separately against `jobstore_url()`.

Run:  pytest tests/unit/test_scheduler.py -v
"""

from __future__ import annotations

import pytest

from jobs.scheduler import (
    CRON,
    DECAY_JOB_ID,
    JOB_IDS,
    REFLECTION_JOB_ID,
    build_scheduler,
    jobstore_url,
)

pytestmark = pytest.mark.timeout(60)


@pytest.fixture
def scheduler():
    sched = build_scheduler(persistent=False)
    try:
        yield sched
    finally:
        if sched.running:  # pragma: no cover - never started here
            sched.shutdown(wait=False)


def test_scheduler_registers_both_cron_jobs(scheduler):
    """Exactly the decay and reflection jobs, each cron, each `max_instances=1`.

    Set EQUALITY, not containment. A third job registered without anyone
    updating `JOB_IDS` fails here — which is the point, because "the scheduler
    has the two jobs I expect *and nothing else*" is the property that keeps a
    stray `add_job` from quietly running against production every minute.
    """
    jobs = {job.id: job for job in scheduler.get_jobs()}

    assert set(jobs) == set(JOB_IDS), (
        f"scheduler registered {sorted(jobs)}, expected exactly {sorted(JOB_IDS)}"
    )
    assert len(jobs) == 2
    assert DECAY_JOB_ID in jobs and REFLECTION_JOB_ID in jobs

    for job_id, job in jobs.items():
        assert type(job.trigger).__name__ == "CronTrigger", (
            f"{job_id} is scheduled with {type(job.trigger).__name__}, not cron"
        )
        assert job.max_instances == 1, (
            f"{job_id} has max_instances={job.max_instances}; two overlapping runs "
            "would each allocate their own decay_run_id and the claim stamp would "
            "stop meaning anything"
        )
        assert job.coalesce is True, f"{job_id} would run once per missed fire"
        assert job.misfire_grace_time == CRON[job_id]["misfire_grace_time"]
        assert callable(job.func)


def test_the_two_jobs_run_different_work(scheduler):
    """Both jobs registered, pointing at *different* callables.

    Cheap to get wrong — one copy-pasted `add_job` line with the wrong function
    gives two jobs, two ids, two triggers, and one job's work never running.
    """
    jobs = {job.id: job for job in scheduler.get_jobs()}
    decay_func = jobs[DECAY_JOB_ID].func
    reflection_func = jobs[REFLECTION_JOB_ID].func

    assert decay_func is not reflection_func
    assert decay_func.__name__ == "scheduled_decay"
    assert reflection_func.__name__ == "scheduled_reflection"
    # Module-level functions, so the persistent jobstore can pickle them by
    # reference. A lambda or a closure would raise at add_job time under
    # SQLAlchemyJobStore — see jobs/scheduler.py.
    assert decay_func.__module__ == "jobs.scheduler"
    assert reflection_func.__module__ == "jobs.scheduler"


def test_reflection_is_less_frequent_than_decay(scheduler):
    """The plan asks for 'nightly decay, less-frequent reflection'."""
    jobs = {job.id: job for job in scheduler.get_jobs()}
    decay_fields = {f.name: str(f) for f in jobs[DECAY_JOB_ID].trigger.fields}
    reflection_fields = {f.name: str(f) for f in jobs[REFLECTION_JOB_ID].trigger.fields}

    assert decay_fields["day_of_week"] == "*", "decay is not nightly"
    assert reflection_fields["day_of_week"] != "*", "reflection fires as often as decay"


def test_persistent_jobstore_is_configured_for_psycopg3():
    """The URL SQLAlchemy will actually be handed.

    `DATABASE_URL` is `postgresql://...`, which SQLAlchemy 2.x resolves to
    psycopg2 — not installed here. The rewrite to `postgresql+psycopg://` is
    what stops the scheduler dying at construction with a
    `ModuleNotFoundError` that reads like a missing dependency.
    """
    url = jobstore_url()
    assert url.startswith("postgresql+psycopg://"), url
    assert "postgresql://" not in url
