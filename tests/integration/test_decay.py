"""Integration tests for the decay job against a live database (M8).

Three plan test cases live here:

    test_claim_batch_uses_skip_locked
    test_row_archived_below_threshold
    test_decay_does_not_undelete_soft_deleted_rows

plus the supporting properties each of them leans on.

NO EMBEDDINGS ARE CREATED BY THIS FILE. The decay path never reads
`memories.embedding`, and embedding the fixture rows would cost minutes of
Voyage rate-limit backoff to produce data nothing under test looks at. Rows are
written straight through the admin connection.

Run:  pytest tests/integration/test_decay.py -v
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from jobs.claims import claim_batch, claim_session, eligible_count, new_run_id
from jobs.decay import archive_rows, archive_threshold, decay_floor, run_decay_worker
from store.db import admin_session

pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

async def _insert(
    subject_id: str,
    content: str,
    *,
    age_days: float = 0.0,
    weight: float = 1.0,
    reinforcement_count: int = 0,
    deleted: bool = False,
) -> str:
    async with admin_session() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO memories
                   (subject_id, actor_id, content, weight, reinforcement_count,
                    last_accessed_at, deleted_at)
            VALUES (%(s)s::uuid, %(s)s::uuid, %(c)s, %(w)s, %(r)s,
                    now() - make_interval(secs => %(age)s),
                    CASE WHEN %(del)s THEN now() ELSE NULL END)
            RETURNING id
            """,
            {
                "s": subject_id,
                "c": content,
                "w": weight,
                "r": reinforcement_count,
                "age": age_days * 86400.0,
                "del": deleted,
            },
        )
        row = await cursor.fetchone()
        return str(row["id"])


async def _row(memory_id: str) -> dict:
    async with admin_session() as conn:
        cursor = await conn.execute(
            "SELECT id, weight, reinforcement_count, deleted_at, archived_at,"
            "       decay_run_id, decay_claimed_at, last_accessed_at, updated_at, content"
            "  FROM memories WHERE id = %s::uuid",
            (memory_id,),
        )
        return dict(await cursor.fetchone())


@pytest.fixture
async def decay_subject():
    """A fresh subject id whose rows are deleted at teardown."""
    subject_id = str(uuid.uuid4())
    try:
        yield subject_id
    finally:
        async with admin_session() as conn:
            await conn.execute(
                "DELETE FROM memories WHERE subject_id = %s::uuid", (subject_id,)
            )


# ---------------------------------------------------------------------------
# test_claim_batch_uses_skip_locked
# ---------------------------------------------------------------------------

async def test_claim_batch_uses_skip_locked(decay_subject):
    """A row locked by another transaction is SKIPPED, not waited for.

    THE CONTROL IS THE POINT. "claim_batch returned in 0.05s" only means
    something if the lock it was supposed to trip over was genuinely held and
    genuinely blocking. So this test does three things in order:

      1. transaction A takes a real `FOR UPDATE` lock on one row;
      2. a *control* transaction proves that lock blocks a plain `FOR UPDATE`
         (with `lock_timeout` set, so it raises rather than hangs the suite);
      3. only then does `claim_batch()` run, and it must come back promptly with
         the OTHER rows and without the locked one.

    Without step 2 this test would pass just as happily against a lock that was
    never taken — which is precisely the "looks like proof and isn't" shape.
    """
    ids = [
        await _insert(decay_subject, f"skiplocked row {i}", age_days=100 - i)
        for i in range(6)
    ]

    # The claim query orders by last_accessed_at, so the STALEST row is the one
    # it would take first. Lock exactly that one: if SKIP LOCKED were missing,
    # the claim would block on its very first row rather than getting lucky.
    async with admin_session() as conn:
        cursor = await conn.execute(
            "SELECT id FROM memories WHERE subject_id = %s::uuid"
            "  AND deleted_at IS NULL ORDER BY last_accessed_at, id LIMIT 1",
            (decay_subject,),
        )
        locked_id = str((await cursor.fetchone())["id"])
    assert locked_id in ids

    run_id = new_run_id()

    async with admin_session() as holder:
        # -- 1. hold the lock -------------------------------------------
        await holder.execute(
            "SELECT id FROM memories WHERE id = %s::uuid FOR UPDATE", (locked_id,)
        )

        # -- 2. control: the lock really does block a plain FOR UPDATE ---
        with pytest.raises(psycopg.errors.LockNotAvailable):
            async with admin_session() as control:
                await control.execute("SET LOCAL lock_timeout = '750ms'")
                await control.execute(
                    "SELECT id FROM memories WHERE id = %s::uuid FOR UPDATE",
                    (locked_id,),
                )

        # -- 3. the claim skips it, promptly ----------------------------
        started = time.perf_counter()
        async with claim_session() as conn:
            claimed = await claim_batch(
                conn, run_id=run_id, batch_size=10, subject_ids=[decay_subject]
            )
        elapsed = time.perf_counter() - started

    claimed_ids = {row.id for row in claimed}

    assert locked_id not in claimed_ids, (
        "claim_batch returned a row another transaction held locked — "
        "FOR UPDATE SKIP LOCKED is not doing its job"
    )
    assert claimed_ids == set(ids) - {locked_id}, (
        f"expected every unlocked row, got {len(claimed_ids)} of {len(ids) - 1}"
    )
    # The control above proves a blocking claim would have waited ~750ms before
    # even erroring; a claim that skipped is a couple of round-trips.
    assert elapsed < 0.75, (
        f"claim_batch took {elapsed:.3f}s. It should skip the locked row "
        "immediately, not wait for it."
    )


async def test_the_skipped_row_is_picked_up_on_the_next_pass(decay_subject):
    """SKIP LOCKED skips *temporarily*. The row must still be eligible after.

    The other half of the guarantee: a claim query that permanently lost skipped
    rows would pass `test_claim_batch_uses_skip_locked` and quietly leave part of
    the table unmaintained forever.
    """
    ids = [
        await _insert(decay_subject, f"revisit row {i}", age_days=50 - i)
        for i in range(4)
    ]
    run_id = new_run_id()

    async with admin_session() as conn:
        cursor = await conn.execute(
            "SELECT id FROM memories WHERE subject_id = %s::uuid"
            " ORDER BY last_accessed_at, id LIMIT 1",
            (decay_subject,),
        )
        locked_id = str((await cursor.fetchone())["id"])

    async with admin_session() as holder:
        await holder.execute(
            "SELECT id FROM memories WHERE id = %s::uuid FOR UPDATE", (locked_id,)
        )
        async with claim_session() as conn:
            first = await claim_batch(
                conn, run_id=run_id, batch_size=10, subject_ids=[decay_subject]
            )
    assert locked_id not in {r.id for r in first}

    # Lock released. Same run id — the row must still be eligible.
    async with claim_session() as conn:
        remaining = await eligible_count(
            conn, run_id=run_id, subject_ids=[decay_subject]
        )
        second = await claim_batch(
            conn, run_id=run_id, batch_size=10, subject_ids=[decay_subject]
        )

    assert remaining == 1
    assert {r.id for r in second} == {locked_id}
    assert {r.id for r in first} | {r.id for r in second} == set(ids)


# ---------------------------------------------------------------------------
# test_row_archived_below_threshold
# ---------------------------------------------------------------------------

async def test_row_archived_below_threshold(decay_subject):
    """A row decayed under ARCHIVE_THRESHOLD gets `archived_at`; one above does not.

    Both directions, in one test, on the same run. Asserting only the archived
    row would pass against `archive_rows()` stamping everything it is handed.
    """
    fresh_id = await _insert(decay_subject, "archived: fresh row", age_days=0.5)
    doomed_id = await _insert(decay_subject, "archived: ancient row", age_days=900)

    stats = await run_decay_worker(worker="archive-test", subject_ids=[decay_subject])
    assert stats.rows_claimed == 2

    fresh = await _row(fresh_id)
    doomed = await _row(doomed_id)

    assert doomed["weight"] < archive_threshold(), (
        f"the ancient row decayed only to {doomed['weight']}, which is not below "
        f"the {archive_threshold()} threshold — the fixture no longer tests archiving"
    )
    assert doomed["archived_at"] is not None, "a row below the threshold was not archived"

    assert fresh["weight"] >= archive_threshold()
    assert fresh["archived_at"] is None, (
        "a row above the threshold was archived — archive_rows is stamping "
        "everything it is given rather than only the flagged rows"
    )

    assert doomed["weight"] >= decay_floor()
    assert stats.rows_archived == 1


async def test_archiving_is_idempotent_and_removes_the_row_from_future_sweeps(
    decay_subject,
):
    """A second sweep must not re-archive or re-claim an archived row."""
    doomed_id = await _insert(decay_subject, "idempotent archive row", age_days=900)

    first = await run_decay_worker(worker="pass-1", subject_ids=[decay_subject])
    stamped_at = (await _row(doomed_id))["archived_at"]
    assert stamped_at is not None
    assert first.rows_archived == 1

    second = await run_decay_worker(worker="pass-2", subject_ids=[decay_subject])
    after = await _row(doomed_id)

    assert second.rows_claimed == 0, "an archived row was claimed again"
    assert second.rows_archived == 0
    assert after["archived_at"] == stamped_at, "archived_at was re-stamped"


async def test_decay_is_idempotent_against_the_database(decay_subject):
    """Running the sweep twice must not decay the same row twice.

    The whole reason `decay_weight()` is a function of age rather than of the
    stored weight. A multiplicative implementation passes every single-run test
    in this file and fails this one.
    """
    memory_id = await _insert(decay_subject, "idempotence row", age_days=40)

    await run_decay_worker(worker="pass-1", subject_ids=[decay_subject])
    after_one = (await _row(memory_id))["weight"]

    # A second sweep needs a fresh run id (a new night), which `run_decay_worker`
    # allocates by default, so the row is eligible again.
    await run_decay_worker(worker="pass-2", subject_ids=[decay_subject])
    after_two = (await _row(memory_id))["weight"]

    assert after_one == pytest.approx(after_two, abs=1e-6), (
        f"weight moved from {after_one} to {after_two} across two runs on the same "
        "night — decay is multiplicative and therefore not idempotent"
    )
    assert after_one < 1.0, "the fixture row did not decay at all"


# ---------------------------------------------------------------------------
# test_decay_does_not_undelete_soft_deleted_rows
# ---------------------------------------------------------------------------

async def test_decay_does_not_undelete_soft_deleted_rows(decay_subject):
    """A soft-deleted row is left completely alone by a decay run.

    Asserts on the WHOLE row, not just `deleted_at`. A job that cleared
    `deleted_at` is the obvious failure, but a job that merely re-weighted or
    archived an erased memory is also wrong: M7's position is that `deleted_at`
    puts a row beyond the reach of everything except the export, and a
    background sweep is not an exception. Comparing the full before/after
    snapshot is what makes this test fail if someone drops
    `deleted_at IS NULL` from the claim predicate in `jobs/claims.py`.
    """
    deleted_id = await _insert(
        decay_subject, "soft deleted row", age_days=900, deleted=True
    )
    live_id = await _insert(decay_subject, "live companion row", age_days=900)

    before = await _row(deleted_id)
    assert before["deleted_at"] is not None, "fixture did not soft-delete the row"

    stats = await run_decay_worker(worker="delete-test", subject_ids=[decay_subject])

    after = await _row(deleted_id)
    companion = await _row(live_id)

    # The companion proves the run actually did work on this subject, so an
    # untouched deleted row means "skipped", not "the job did nothing".
    assert companion["weight"] < 1.0
    assert companion["archived_at"] is not None
    assert stats.rows_claimed == 1, (
        f"the sweep claimed {stats.rows_claimed} rows; only the live one should "
        "have been eligible"
    )
    assert deleted_id not in stats.processed_ids

    assert after == before, (
        "a soft-deleted row was modified by the decay run. Changed fields: "
        + str({k: (before[k], after[k]) for k in before if before[k] != after[k]})
    )
    assert after["deleted_at"] is not None
    assert after["archived_at"] is None
    assert after["decay_run_id"] is None
    assert after["weight"] == before["weight"]


async def test_archive_rows_refuses_a_soft_deleted_row_even_when_asked(decay_subject):
    """The second guard, tested directly.

    `archive_rows()` carries `AND deleted_at IS NULL` even though the claim
    predicate already makes an erased row unreachable. This calls it with the id
    anyway — the only way to exercise a defence-in-depth guard is to bypass the
    layer in front of it.
    """
    deleted_id = await _insert(
        decay_subject, "directly archived deleted row", age_days=900, deleted=True
    )

    async with claim_session() as conn:
        archived = await archive_rows(conn, [deleted_id])

    assert archived == [], "archive_rows stamped a soft-deleted row"
    row = await _row(deleted_id)
    assert row["archived_at"] is None
    assert row["deleted_at"] is not None


# ---------------------------------------------------------------------------
# the properties the drain loop depends on
# ---------------------------------------------------------------------------

async def test_a_sweep_drains_the_table_and_stops(decay_subject):
    """The loop terminates because a stamped row stops being eligible."""
    for i in range(25):
        await _insert(decay_subject, f"drain row {i}", age_days=i * 3)

    stats = await run_decay_worker(
        worker="drain", size=4, subject_ids=[decay_subject]
    )

    assert stats.rows_claimed == 25
    assert len(set(stats.processed_ids)) == 25
    assert stats.batches == 7  # ceil(25/4) claims + one empty claim to stop
    assert stats.error is None

    async with claim_session() as conn:
        left = await eligible_count(
            conn, run_id=stats.run_id, subject_ids=[decay_subject]
        )
    assert left == 0


async def test_decay_does_not_touch_updated_at(decay_subject):
    """`updated_at` means 'the content changed'. Aging a weight is not that."""
    memory_id = await _insert(decay_subject, "updated_at row", age_days=100)
    before = await _row(memory_id)

    await asyncio.sleep(0.05)
    await run_decay_worker(worker="updated-at", subject_ids=[decay_subject])
    after = await _row(memory_id)

    assert after["weight"] < before["weight"], "the row did not decay"
    assert after["updated_at"] == before["updated_at"], (
        "the decay run bumped updated_at, which would make every row in the "
        "store look freshly edited after the first nightly sweep"
    )
    assert after["content"] == before["content"]
    assert after["last_accessed_at"] == before["last_accessed_at"]


async def test_the_claim_query_is_cross_subject_by_default(decay_subject):
    """With no `subject_ids`, the sweep spans subjects — the reason it is an
    owner-connection job (see `jobs/claims.py`).

    Deliberately does NOT run a global sweep (that would age unrelated rows on a
    shared development database). It asserts the weaker, sufficient thing: two
    different subjects' rows are both visible to one claim on the owner
    connection, which is exactly what RLS would prevent on the app connection.
    """
    other_subject = str(uuid.uuid4())
    mine = await _insert(decay_subject, "cross-subject: mine", age_days=10)
    theirs = await _insert(other_subject, "cross-subject: theirs", age_days=11)

    try:
        async with claim_session() as conn:
            claimed = await claim_batch(
                conn,
                run_id=new_run_id(),
                batch_size=10,
                subject_ids=[decay_subject, other_subject],
            )
        claimed_ids = {r.id for r in claimed}
        subjects = {r.subject_id for r in claimed}

        assert {mine, theirs} <= claimed_ids
        assert subjects == {decay_subject, other_subject}, (
            "one claim returned rows for only one subject; the sweep is not "
            "cross-subject and the owner-connection carve-out is unjustified"
        )
    finally:
        async with admin_session() as conn:
            await conn.execute(
                "DELETE FROM memories WHERE subject_id = %s::uuid", (other_subject,)
            )
