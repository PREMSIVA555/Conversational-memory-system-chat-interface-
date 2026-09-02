"""The cooperative claim query (M8 step 2).

    SELECT id FROM memories
     WHERE <decay eligible>
     ORDER BY last_accessed_at
     LIMIT :n
       FOR UPDATE SKIP LOCKED

and, in the SAME statement and therefore the same transaction, stamp the
claimed rows with the current `decay_run_id`. That combination is the whole
concurrency design, and each half is load-bearing:

`FOR UPDATE`        takes a row lock, so no two transactions can hold the same
                    row at once.
`SKIP LOCKED`       makes a second worker step over rows another worker is
                    already holding instead of *blocking* on them. Without it
                    three workers would serialise into one: worker B would wait
                    for worker A's whole batch to commit before it saw anything.
`decay_run_id`      makes the claim durable past the transaction. The row lock
                    disappears at COMMIT; the stamp is what stops the same
                    worker (or another one) re-claiming the row on the next
                    iteration of the drain loop.
`ORDER BY
 last_accessed_at`  stalest first, which is the order that matters for a job
                    whose purpose is aging things out — and it makes a partial
                    run a *useful* partial run rather than an arbitrary one.

Written as a single `UPDATE ... FROM (CTE) ... RETURNING` rather than a SELECT
followed by an UPDATE. Two statements would leave a window between the read and
the mark in which the transaction is holding locks but the rows are not yet
stamped; the single statement has no such window, and `RETURNING` hands back
exactly the rows that were actually marked, which is the honest definition of
"claimed".


THE RLS DECISION — READ THIS BEFORE CHANGING THE CONNECTION
===========================================================
`memories` has FORCE ROW LEVEL SECURITY and every policy is scoped on
`app.subject_id` AND `app.actor_id`. The decay job is **cross-subject by
design**: it ages every subject's rows in one pass, ordered globally by
staleness. There is no single (subject, actor) pair it could set, so on the
application connection this query returns zero rows — RLS fails closed, exactly
as 0004 intends.

So `claim_session()` opens the **owner** connection (`store.db.admin_session()`,
`DATABASE_URL`), which is a superuser and therefore bypasses RLS. That is a
deliberate, narrow carve-out, and it is the same one M7 already established:
`0006_audit_append_only.sql` exempts the owner explicitly as "the
migration/maintenance role ... retention pruning, test-fixture cleanup and
schema surgery have to remain possible for *someone*". Decay is precisely that
category of work.

The boundary is drawn here rather than everywhere:

  * ONLY the decay path uses the owner connection, and only to read `weight`,
    `reinforcement_count`, `last_accessed_at` and to write `weight`,
    `archived_at`, `decay_run_id`, `decay_claimed_at`. It never reads `content`,
    never reads `embedding`, and never writes a row that did not already exist.
  * The REFLECTION job, which *does* touch content and *does* create rows, runs
    entirely on the application connection through `store.db.session()` with the
    subject's GUCs set, inside RLS, like every other writer. See
    `jobs/reflection.py`.
  * The API, the capture worker and retrieval are unchanged and unaffected.

WHAT I WOULD PREFER, and why it is not here: a dedicated `memory_decay` login
role — NOSUPERUSER, BYPASSRLS, granted only `SELECT, UPDATE(weight,
archived_at, decay_run_id, decay_claimed_at)` on `memories` — is a strictly
tighter boundary than "the superuser that also owns the schema", because it
cannot read `content` at all. Creating it needs a new credential in
`infra/.env`, which this milestone is not permitted to write. It is reported as
a follow-up rather than silently skipped.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Sequence

from store.db import admin_dsn, get_pool

__all__ = [
    "ClaimedRow",
    "new_run_id",
    "claim_session",
    "claim_batch",
    "eligible_count",
]


def new_run_id() -> str:
    """A fresh decay run id. Every worker in one run must share it."""
    return str(uuid.uuid4())


@dataclass(slots=True, frozen=True)
class ClaimedRow:
    """One row this worker now owns for the remainder of its transaction."""

    id: str
    subject_id: str
    weight: float
    reinforcement_count: int
    last_accessed_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ClaimedRow":
        return cls(
            id=str(row["id"]),
            subject_id=str(row["subject_id"]),
            weight=float(row["weight"]),
            reinforcement_count=int(row["reinforcement_count"]),
            last_accessed_at=row["last_accessed_at"],
        )


@asynccontextmanager
async def claim_session() -> AsyncIterator[Any]:
    """An owner connection inside an EXPLICIT transaction. See the RLS note above.

    The transaction is explicit (rather than relying on the pool's implicit
    one) because the batch boundary and the transaction boundary must be the
    same thing: a worker that dies mid-batch must have its claims rolled back so
    another worker picks those rows up, and a worker that finishes a batch must
    release its locks before starting the next one.
    """
    pool = await get_pool(admin_dsn())
    async with pool.connection() as conn:
        async with conn.transaction():
            yield conn


# The claim predicate, in one place so the query and `eligible_count()` cannot
# drift apart. `%(run_id)s` is the run being drained.
#
#   deleted_at IS NULL   a soft-deleted row is out of the job's reach entirely.
#                        M7 says `deleted_at` is the user's erasure signal, and
#                        a maintenance job has no business writing to a row the
#                        user has erased — not even its weight. This predicate,
#                        not a defensive UPDATE clause, is what makes
#                        `test_decay_does_not_undelete_soft_deleted_rows` true.
#   archived_at IS NULL  archiving is terminal for this job. Re-claiming
#                        archived rows every night would make the drain loop
#                        never shrink.
#   decay_run_id IS
#     DISTINCT FROM      already handled by some worker in THIS run. `IS
#                        DISTINCT FROM` rather than `<>` so a NULL (never
#                        decayed) row compares as eligible — `NULL <> 'x'` is
#                        NULL, which is not TRUE, which would hide every fresh
#                        row from the job forever.
_ELIGIBLE = """
      deleted_at IS NULL
  AND archived_at IS NULL
  AND decay_run_id IS DISTINCT FROM %(run_id)s::uuid
"""

_CLAIM_SQL = f"""
WITH claimed AS (
    SELECT id
      FROM memories
     WHERE {_ELIGIBLE}
       {{subject_filter}}
     ORDER BY last_accessed_at, id
     LIMIT %(batch_size)s
     FOR UPDATE SKIP LOCKED
)
UPDATE memories m
   SET decay_run_id     = %(run_id)s::uuid,
       decay_claimed_at = now()
  FROM claimed c
 WHERE m.id = c.id
RETURNING m.id, m.subject_id, m.weight, m.reinforcement_count, m.last_accessed_at
"""

_COUNT_SQL = f"""
SELECT count(*) AS n
  FROM memories
 WHERE {_ELIGIBLE}
   {{subject_filter}}
"""

_SUBJECT_FILTER = "AND subject_id = ANY(%(subject_ids)s::uuid[])"


async def claim_batch(
    conn: Any,
    *,
    run_id: str,
    batch_size: int,
    subject_ids: Sequence[str] | None = None,
) -> list[ClaimedRow]:
    """Claim up to `batch_size` eligible rows on `conn`. Empty list when drained.

    `conn` MUST already be inside a transaction (use `claim_session()`), because
    the row locks this takes are only meaningful for as long as that transaction
    lives. Handing in an autocommit connection would take the locks and drop
    them again before the caller had done anything with the rows.

    `subject_ids` narrows the sweep to a set of subjects. Production passes
    None — the sweep is global, which is the entire reason this runs on the
    owner connection. Tests pass their own fixture subjects so that a run on a
    shared development database cannot age somebody else's rows; the predicate
    is otherwise character-for-character the same query, including the
    `FOR UPDATE SKIP LOCKED`.
    """
    params: dict[str, Any] = {"run_id": str(run_id), "batch_size": int(batch_size)}
    subject_filter = ""
    if subject_ids is not None:
        params["subject_ids"] = [str(s) for s in subject_ids]
        subject_filter = _SUBJECT_FILTER

    cursor = await conn.execute(_CLAIM_SQL.format(subject_filter=subject_filter), params)
    return [ClaimedRow.from_row(row) for row in await cursor.fetchall()]


async def eligible_count(
    conn: Any, *, run_id: str, subject_ids: Sequence[str] | None = None
) -> int:
    """How many rows this run has left to claim. Diagnostics and tests only."""
    params: dict[str, Any] = {"run_id": str(run_id)}
    subject_filter = ""
    if subject_ids is not None:
        params["subject_ids"] = [str(s) for s in subject_ids]
        subject_filter = _SUBJECT_FILTER

    cursor = await conn.execute(_COUNT_SQL.format(subject_filter=subject_filter), params)
    row = await cursor.fetchone()
    return int(row["n"]) if row else 0
