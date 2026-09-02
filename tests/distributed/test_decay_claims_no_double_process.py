"""M8's headline test: three real decay workers, one table, no row touched twice.

    pytest tests/distributed/test_decay_claims_no_double_process.py -s

`-s` (or `-o log_cli=true`) is worth it here — the Definition of Done asks you to
confirm from the output that three worker processes really ran, and the worker
and pid of each is printed by `test_three_real_worker_processes_ran`.

WHAT MAKES THIS A REAL TEST AND NOT A GREEN RECTANGLE
-----------------------------------------------------
Three separate claims, and each one fails under a different mutation of
`jobs/claims.py`:

  no row processed twice          remove `decay_run_id IS DISTINCT FROM :run_id`
                                  from the claim predicate and rows are claimed
                                  again on the next iteration; `RunStats.
                                  add_processed()` raises on the repeat and the
                                  worker exits non-zero.

  union == the whole fixture      the fixture's ids are known up front, so a row
                                  that SKIP LOCKED skipped and nobody came back
                                  for shows up as a missing id rather than as a
                                  count that happens to look plausible.

  at least two workers claimed    without this, "worker-0 did all 300 rows while
                                  the others found an empty table" passes every
                                  correctness assertion above while exercising
                                  no concurrency at all. The start barrier in
                                  `conftest.spawn_workers` is what makes this
                                  assertion deterministic rather than a race.

The three workers share ONE `--run-id`, which is what makes them one sweep
rather than three independent ones. Three different run ids and every worker
would consider every row eligible.
"""

from __future__ import annotations

import os

import pytest

from store.db import admin_session

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

WORKER_COUNT = 3


@pytest.fixture(scope="function")
async def workers(decay_fixture, spawn_decay_workers, tmp_path):
    """Run the three-worker sweep once; every test in this module reads it."""
    return spawn_decay_workers(
        count=WORKER_COUNT,
        run_id=decay_fixture.run_id,
        subject_ids=[decay_fixture.subject_id],
        out_dir=tmp_path / "workers",
    )


def test_three_real_worker_processes_ran(workers, decay_fixture, capsys):
    """Definition of Done: confirm three concurrent worker PROCESSES actually ran.

    Distinct pids, none of them this test's own, each having logged its own
    start line. Printed so the DoD's "check the worker/pid log lines" is
    answerable by reading the test output.
    """
    with capsys.disabled():
        print()
        print(f"  fixture subject : {decay_fixture.subject_id}")
        print(f"  fixture rows    : {decay_fixture.row_count}")
        print(f"  shared run_id   : {decay_fixture.run_id}")
        print(f"  pytest pid      : {os.getpid()}")
        for result in workers:
            print(
                f"  {result.worker}  pid={result.pid}  "
                f"claimed={result.stats['rows_claimed']}  "
                f"decayed={result.stats['rows_decayed']}  "
                f"archived={result.stats['rows_archived']}  "
                f"batches={result.stats['batches']}  "
                f"{result.stats['duration_seconds']}s"
            )
        for result in workers:
            for line in result.output.splitlines():
                if "decay.worker.start" in line or "decay.run.complete" in line:
                    print(f"  [{result.worker}] {line.strip()}")

    assert len(workers) == WORKER_COUNT

    pids = [r.pid for r in workers]
    assert len(set(pids)) == WORKER_COUNT, f"workers shared a pid: {pids}"
    assert os.getpid() not in pids, "a 'worker' ran in the pytest process"
    for result in workers:
        assert result.pid > 0
        assert "decay.worker.start" in result.output, (
            f"{result.worker} never logged a start line"
        )
        assert f'"pid": {result.pid}' in result.output, (
            f"{result.worker}'s log lines do not carry its own pid {result.pid}"
        )
        assert result.stats["run_id"] == decay_fixture.run_id


def test_decay_claims_no_double_process(workers, decay_fixture):
    """No row was processed twice — not within a worker, not across workers."""
    per_worker = {r.worker: r.processed_ids for r in workers}

    # within a worker
    for worker, ids in per_worker.items():
        assert len(ids) == len(set(ids)), (
            f"{worker} processed a row twice: "
            f"{[i for i in ids if ids.count(i) > 1][:5]}"
        )

    # across workers — every pairwise intersection must be empty
    names = list(per_worker)
    for i, left in enumerate(names):
        for right in names[i + 1:]:
            overlap = set(per_worker[left]) & set(per_worker[right])
            assert not overlap, (
                f"{left} and {right} both processed {len(overlap)} row(s), "
                f"e.g. {sorted(overlap)[:5]}. FOR UPDATE SKIP LOCKED is not "
                f"isolating claims."
            )

    total = sum(len(ids) for ids in per_worker.values())
    assert total == decay_fixture.row_count, (
        f"{total} rows processed across all workers, fixture has "
        f"{decay_fixture.row_count}"
    )


def test_all_rows_processed_exactly_once_total(workers, decay_fixture):
    """The union of the three workers' id sets is EXACTLY the fixture set.

    This is the half `test_decay_claims_no_double_process` cannot see. SKIP
    LOCKED is allowed to skip a row — that is its whole job — but only
    temporarily: some worker must come back for it. A permanently skipped row
    leaves the totals looking fine (nothing was double-processed) while the
    table is quietly under-maintained.
    """
    union: set[str] = set()
    for result in workers:
        union |= result.processed_set

    missing = decay_fixture.id_set - union
    extra = union - decay_fixture.id_set

    assert not missing, (
        f"{len(missing)} fixture row(s) were never processed by any worker — "
        f"SKIP LOCKED skipped them permanently. e.g. {sorted(missing)[:5]}"
    )
    assert not extra, (
        f"{len(extra)} row(s) outside the fixture were processed; the sweep is "
        f"not scoped to the test subject. e.g. {sorted(extra)[:5]}"
    )
    assert union == decay_fixture.id_set


def test_the_sweep_was_actually_concurrent(workers):
    """At least two of the three workers claimed rows.

    Without this the module would pass with one worker doing everything and the
    other two finding an empty table — every correctness assertion above holds
    in that world, and none of them would be testing SKIP LOCKED. The start
    barrier in `conftest.spawn_workers` is what makes this deterministic.
    """
    contributed = [r.worker for r in workers if r.processed_ids]
    assert len(contributed) >= 2, (
        "only " + (contributed[0] if contributed else "no worker") + " claimed any "
        "rows, so no two transactions ever competed for one. Check that the start "
        "barrier fired: "
        + str({r.worker: len(r.processed_ids) for r in workers})
    )


async def test_every_fixture_row_carries_the_shared_run_id(workers, decay_fixture):
    """The database agrees with the workers' self-reports.

    `processed_ids` is what each worker *says* it did. This reads the table
    back: every fixture row must be stamped with the one shared `decay_run_id`,
    which is the durable record of the claim and the thing that stops a second
    sweep re-claiming them. Asserting only on the JSON files would let a worker
    that logged perfectly and wrote nothing pass.
    """
    async with admin_session() as conn:
        cursor = await conn.execute(
            "SELECT count(*) AS n,"
            "       count(*) FILTER (WHERE decay_run_id = %(run_id)s::uuid) AS stamped,"
            "       count(*) FILTER (WHERE decay_claimed_at IS NOT NULL) AS claimed,"
            "       count(*) FILTER (WHERE weight < 1.0) AS decayed,"
            "       count(*) FILTER (WHERE archived_at IS NOT NULL) AS archived"
            "  FROM memories WHERE subject_id = %(subject_id)s::uuid",
            {"run_id": decay_fixture.run_id, "subject_id": decay_fixture.subject_id},
        )
        row = await cursor.fetchone()

    assert row["n"] == decay_fixture.row_count
    assert row["stamped"] == decay_fixture.row_count, (
        f"only {row['stamped']}/{row['n']} rows carry the shared run id"
    )
    assert row["claimed"] == decay_fixture.row_count
    # The fixture ages rows across 400 days, so almost all of them lose weight
    # and a real share cross ARCHIVE_THRESHOLD. If these were zero the workers
    # would have claimed rows and done nothing to them.
    assert row["decayed"] > 0, "no row's weight changed — the apply node did nothing"
    assert row["archived"] > 0, "no row was archived — the archive node did nothing"
