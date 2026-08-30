"""Persistence for `memories` rows: insert, reinforce, and the dedup query.

Everything here runs inside `store.db.session()`, which sets the
``app.subject_id`` / ``app.actor_id`` GUCs that M1's RLS policies read. The app
role is NOSUPERUSER / NOBYPASSRLS against a table with FORCE ROW LEVEL
SECURITY, so a write attempted without those GUCs is rejected outright with
*new row violates row-level security policy*. That is the design working, not a
bug to route around.

CONCURRENCY -----------------------------------------------------------------
`persist_candidates()` is the only sanctioned write path, and it is not a
read-then-write race. The sequence for one subject is:

    BEGIN
      pg_advisory_xact_lock(hashtext(subject_id))   -- serialise this subject
      for each candidate:
          find_similar(...)                          -- sees everything committed
          INSERT or UPDATE                           -- visible to the next loop
    COMMIT                                           -- lock released

Two identical turns arriving at the same instant therefore cannot both observe
"no similar row" and both insert. The second transaction blocks on the advisory
lock until the first commits, then its `find_similar` sees the freshly inserted
row and reinforces it instead. The lock is transaction-scoped, so it is always
released -- including on rollback or a dropped connection -- with no unlock
bookkeeping to get wrong.

The same mechanism gives intra-turn dedup for free: all of a turn's candidates
are persisted inside one transaction, so a second candidate that restates the
first sees it already inserted.

An advisory lock is used rather than a unique constraint on purpose: duplicates
here are *semantic* (cosine similarity over embeddings), not lexical, so there
is no column tuple a UNIQUE index could cover. It also needs no migration --
`store/migrations/` is out of this milestone's scope.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

from capture import config as capture_config
from store.db import session

# ---------------------------------------------------------------------------
# vector marshalling
# ---------------------------------------------------------------------------


def to_vector_literal(vector: Sequence[float]) -> str:
    """Render a Python float sequence as a pgvector literal.

    psycopg has no native adapter for the `vector` type, so vectors travel as
    text and are cast with `%s::vector` at the call site. `repr(float(x))` keeps
    full round-trip precision -- str() would silently truncate.
    """
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


async def find_similar(
    conn: Any,
    subject_id: str,
    embedding: Sequence[float],
    *,
    limit: int = 1,
) -> list[dict[str, Any]]:
    """Nearest existing memories to `embedding`, most similar first.

    Two filters are stated explicitly in the SQL even though RLS would also
    enforce the first:

      ``subject_id = %s``     one subject's facts can never dedup against
                              another's. RLS enforces this too, but relying on
                              RLS alone would mean the query is only correct as
                              long as every future caller remembers to open the
                              right session. Belt and braces --
                              `test_dedup_scoped_to_subject_id` is the guard.
      ``deleted_at IS NULL``  a soft-deleted memory (M7) must not silently
                              resurrect itself by absorbing a new fact as a
                              "duplicate".

    `<=>` is pgvector's cosine *distance*, so similarity is `1 - distance`.
    """
    literal = to_vector_literal(embedding)
    cursor = await conn.execute(
        """
        SELECT id,
               content,
               reinforcement_count,
               1 - (embedding <=> %s::vector) AS similarity
          FROM memories
         WHERE subject_id = %s
           AND deleted_at IS NULL
           AND embedding IS NOT NULL
         ORDER BY embedding <=> %s::vector
         LIMIT %s
        """,
        (literal, subject_id, literal, limit),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def get_memory(conn: Any, memory_id: str) -> Optional[dict[str, Any]]:
    cursor = await conn.execute("SELECT * FROM memories WHERE id = %s", (memory_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_memories(subject_id: str, actor_id: str) -> list[dict[str, Any]]:
    """All live memories for a subject, newest first. Convenience for tests/CLI."""
    async with session(subject_id, actor_id) as conn:
        cursor = await conn.execute(
            """
            SELECT id, subject_id, actor_id, content, source, importance, confidence,
                   weight, reinforcement_count, created_at, updated_at,
                   last_accessed_at, deleted_at
              FROM memories
             WHERE subject_id = %s AND deleted_at IS NULL
             ORDER BY created_at DESC
            """,
            (subject_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


async def insert_memory(
    subject_id: str,
    actor_id: str,
    content: str,
    embedding: Sequence[float] | None,
    source: str | None,
    importance: float | None,
    confidence: float | None,
    *,
    conn: Any = None,
) -> str:
    """Insert one memory row and return its id (plan step 8).

    `content` must already be the PII-redacted text -- this function is the last
    stop before the column and does no scrubbing of its own; `capture/pii.py`
    owns that and runs four nodes earlier.

    With `conn=None` the write opens its own RLS-scoped session. Passing an
    existing `conn` lets `persist_candidates()` keep the similarity check and
    the insert inside one transaction (and one advisory lock). Either way the
    statement executes with the RLS GUCs set.
    """
    if conn is None:
        async with session(subject_id, actor_id) as own_conn:
            return await insert_memory(
                subject_id, actor_id, content, embedding, source,
                importance, confidence, conn=own_conn,
            )

    literal = to_vector_literal(embedding) if embedding is not None else None
    cursor = await conn.execute(
        """
        INSERT INTO memories
               (subject_id, actor_id, content, embedding, source, importance, confidence)
        VALUES (%s, %s, %s, %s::vector, %s, %s, %s)
        RETURNING id
        """,
        (subject_id, actor_id, content, literal, source, importance, confidence),
    )
    row = await cursor.fetchone()
    return str(row["id"])


async def reinforce(
    memory_id: str,
    *,
    subject_id: str | None = None,
    actor_id: str | None = None,
    conn: Any = None,
) -> Optional[dict[str, Any]]:
    """Strengthen an existing memory instead of inserting a duplicate (plan step 7).

    Bumps `reinforcement_count`, raises `weight` (capped, so a repeated fact
    cannot dominate M4's ranking), and refreshes `updated_at` /
    `last_accessed_at`. It performs **no INSERT** -- that is the entire point of
    the reinforcement path, and `test_duplicate_fact_reinforces_single_row`
    asserts the row count stays at one.

    Returns the updated row, or None if RLS filtered it out (i.e. the caller
    does not own it) -- the UPDATE simply matches zero rows in that case.
    """
    if conn is None:
        if subject_id is None or actor_id is None:
            raise ValueError("reinforce() needs subject_id/actor_id when no conn is supplied")
        async with session(subject_id, actor_id) as own_conn:
            return await reinforce(memory_id, conn=own_conn)

    cursor = await conn.execute(
        """
        UPDATE memories
           SET reinforcement_count = reinforcement_count + 1,
               weight              = LEAST(weight + %s, %s),
               updated_at          = now(),
               last_accessed_at    = now()
         WHERE id = %s
           AND deleted_at IS NULL
        RETURNING id, reinforcement_count, weight
        """,
        (capture_config.weight_increment(), capture_config.weight_max(), memory_id),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# the guarded write path
# ---------------------------------------------------------------------------

# hashtext() is stable per database and returns int4, which is exactly what the
# single-argument advisory lock functions take.
_SUBJECT_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext(%s))"

# `lock_timeout` applies to advisory-lock waits too, and `is_local => true`
# scopes it to this transaction so it never leaks to the next borrower of the
# pooled connection.
#
# WHY BOUND IT AT ALL: the lock is held only across a similarity query and one
# INSERT/UPDATE -- never across a provider call -- so a legitimate wait is
# milliseconds. An unbounded wait would turn any wedged transaction elsewhere
# into a capture worker that blocks forever and a test suite that stalls
# instead of failing. With the bound, Postgres cancels the statement and the
# job fails loudly through the worker's error path.
_LOCK_TIMEOUT_SQL = "SELECT set_config('lock_timeout', %s, true)"


async def persist_candidates(
    subject_id: str,
    actor_id: str,
    candidates: Iterable[Any],
    *,
    threshold: float | None = None,
) -> list[dict[str, Any]]:
    """Insert-or-reinforce every candidate atomically for this subject.

    Returns one result dict per candidate:
        {text, action: "insert"|"reinforce", memory_id, similarity,
         dedup_status_at_write, dedup_status_from_node}

    The dedup decision is made **here**, under the advisory lock, not taken on
    trust from `capture/dedup.py`. The dedup node's verdict is computed outside
    any lock and is therefore advisory only -- it is recorded as
    `dedup_status_from_node` for observability, and a disagreement between the
    two is logged. The value used to decide is always the one read under the
    lock.
    """
    items = list(candidates)
    if not items:
        return []

    limit = threshold if threshold is not None else capture_config.dedup_cosine_threshold()
    results: list[dict[str, Any]] = []

    async with session(subject_id, actor_id) as conn:
        # Serialise every capture write for this subject. Released at COMMIT.
        await conn.execute(_LOCK_TIMEOUT_SQL, (f"{capture_config.lock_timeout_ms()}ms",))
        await conn.execute(_SUBJECT_LOCK_SQL, (str(subject_id),))

        for candidate in items:
            embedding = getattr(candidate, "embedding", None)
            text = getattr(candidate, "text", "")

            match: dict[str, Any] | None = None
            if embedding:
                rows = await find_similar(conn, subject_id, embedding, limit=1)
                if rows and rows[0]["similarity"] is not None and rows[0]["similarity"] >= limit:
                    match = rows[0]

            if match is not None:
                await reinforce(str(match["id"]), conn=conn)
                results.append(
                    {
                        "text": text,
                        "action": "reinforce",
                        "memory_id": str(match["id"]),
                        "similarity": float(match["similarity"]),
                        "dedup_status_at_write": "duplicate",
                        "dedup_status_from_node": getattr(candidate, "dedup_status", None),
                    }
                )
            else:
                memory_id = await insert_memory(
                    subject_id,
                    actor_id,
                    text,
                    embedding,
                    getattr(candidate, "source", None),
                    getattr(candidate, "importance", None),
                    getattr(candidate, "confidence", None),
                    conn=conn,
                )
                results.append(
                    {
                        "text": text,
                        "action": "insert",
                        "memory_id": memory_id,
                        "similarity": getattr(candidate, "similarity", None),
                        "dedup_status_at_write": "new",
                        "dedup_status_from_node": getattr(candidate, "dedup_status", None),
                    }
                )

    return results
