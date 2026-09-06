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

AUDIT (M7 step 3) ------------------------------------------------------------
`persist_candidates()` is also the *only* place the capture path writes a
`write` audit row, and it does so on the same connection, inside the same
transaction and under the same advisory lock as the insert/reinforce it
describes. Do not add a second emission in `capture/write.py` -- that node is
one call frame further out, outside this transaction, and hooking it too would
produce two audit rows per memory. See `store/audit.py` on the guard that also
catches this at runtime.

An advisory lock is used rather than a unique constraint on purpose: duplicates
here are *semantic* (cosine similarity over embeddings), not lexical, so there
is no column tuple a UNIQUE index could cover. It also needs no migration --
`store/migrations/` is out of this milestone's scope.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

from capture import config as capture_config
from capture.attributes import is_single_valued
from capture.metrics import log_event
from store.audit import UPDATE, WRITE, write_audit
import json

from store.db import admin_session, session

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
      ``superseded_at IS NULL``  nor may a preference the user has since changed
                              (M9). Absorbing "prefers C++" into the superseded
                              "prefers Java" row would undo the supersession and
                              restore exactly the bug it was built to fix.

    `attribute` is selected because `persist_candidates` needs it: a near-match
    that fills the SAME single-valued slot with a DIFFERENT value is a
    supersession, not a duplicate.

    `<=>` is pgvector's cosine *distance*, so similarity is `1 - distance`.
    """
    literal = to_vector_literal(embedding)
    cursor = await conn.execute(
        """
        SELECT id,
               content,
               attribute,
               reinforcement_count,
               1 - (embedding <=> %s::vector) AS similarity
          FROM memories
         WHERE subject_id = %s
           AND deleted_at IS NULL
           AND superseded_at IS NULL
           AND embedding IS NOT NULL
         ORDER BY embedding <=> %s::vector
         LIMIT %s
        """,
        (literal, subject_id, literal, limit),
    )
    return [dict(row) for row in await cursor.fetchall()]


_TOMBSTONE_SQL = """
SELECT id,
       content,
       deleted_at,
       superseded_at,
       1 - (embedding <=> %s::vector) AS similarity
  FROM memories
 WHERE subject_id = %s
   AND embedding IS NOT NULL
   AND (deleted_at IS NOT NULL OR superseded_at IS NOT NULL)
 ORDER BY embedding <=> %s::vector
 LIMIT %s
"""


async def find_similar_tombstones(
    conn: Any,
    subject_id: str,
    embedding: Sequence[float],
    *,
    limit: int = 1,
) -> list[dict[str, Any]]:
    """Nearest DELETED or SUPERSEDED memories — the mirror of `find_similar`.

    `find_similar` deliberately hides these, because a retired row must never
    absorb a new fact as a "duplicate". This exists for the opposite question:
    "is this candidate something the user already got rid of?"

    Used only for candidates the ASSISTANT produced. The asymmetry is the whole
    point and is spelled out in `persist_candidates`: a user restating a fact
    means they still hold it, so it should come back. The assistant restating a
    fact means only that it read the fact somewhere — quite possibly from a
    summary that had not yet been invalidated — and letting that recreate an
    erased memory is how a deleted preference resurrects itself.
    """
    literal = to_vector_literal(embedding)
    cursor = await conn.execute(
        _TOMBSTONE_SQL, (literal, subject_id, literal, limit)
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
    attribute: str | None = None,
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
                importance, confidence, attribute=attribute, conn=own_conn,
            )

    literal = to_vector_literal(embedding) if embedding is not None else None
    cursor = await conn.execute(
        """
        INSERT INTO memories
               (subject_id, actor_id, content, embedding, source, importance,
                confidence, attribute)
        VALUES (%s, %s, %s, %s::vector, %s, %s, %s, %s)
        RETURNING id
        """,
        (subject_id, actor_id, content, literal, source, importance, confidence,
         attribute),
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


_STALE_ANY_ACTOR_SQL = """
UPDATE memories AS summary
   SET stale_at   = now(),
       updated_at = now()
  FROM memories AS src
 WHERE src.id             = %(source_id)s::uuid
   AND src.subject_id     = %(subject_id)s::uuid
   AND summary.id         = src.consolidated_into
   AND summary.subject_id = %(subject_id)s::uuid
   AND summary.deleted_at IS NULL
   AND summary.stale_at   IS NULL
RETURNING summary.id, summary.content, summary.actor_id
"""

_STALE_SQL = """
UPDATE memories AS summary
   SET stale_at   = now(),
       updated_at = now()
  FROM memories AS src
 WHERE src.id                = %(source_id)s::uuid
   AND summary.id            = src.consolidated_into
   AND summary.subject_id    = %(subject_id)s::uuid
   AND summary.deleted_at    IS NULL
   AND summary.stale_at      IS NULL
RETURNING summary.id, summary.content
"""


async def invalidate_summaries_for(
    *,
    subject_id: str,
    actor_id: str,
    source_id: str,
    reason: str,
) -> list[dict[str, Any]]:
    """Invalidate a source's summary ACROSS ACTORS. Call before the mutation.

    WHY THIS EXISTS SEPARATELY FROM `mark_summary_stale`
    ----------------------------------------------------
    A cold verifier found an erasure hole. `mark_summary_stale` runs in the
    caller's RLS session, and BOTH policies on `memories` are scoped on
    `actor_id` as well as `subject_id`:

        memories_select_own  subject_id = app.subject_id AND actor_id = app.actor_id
        memories_update_own  subject_id = app.subject_id AND actor_id = app.actor_id

    A summary written under a DIFFERENT actor - the reflection job's actor, say,
    or another client of the same subject - is therefore invisible to the
    deleting caller. The UPDATE matched zero rows, the function returned `[]`,
    and that is indistinguishable from "this source had no summary". The DELETE
    returned 200 and the audit trail recorded a successful erasure while the
    erased fact stayed retrievable.

    Note the asymmetry that made it silent: `ensure_owned` authorises on
    `subject_id` alone, so the delete is legitimate, while the cascade is gated
    on `actor_id` too. Ownership is checked on one axis and propagation on
    another.

    So this runs as the owner, where RLS cannot hide a row from the subject's
    own erasure. That is a real privilege escalation and it is deliberately
    narrow: it can only ever set `stale_at` on a summary belonging to the
    subject named in the argument, and only for a source that also belongs to
    them - the WHERE clause checks both.

    ORDERING MATTERS AND IS PART OF THE FIX. Callers invoke this BEFORE the
    mutation, in its own transaction. If invalidation fails, the caller aborts
    and nothing is deleted - the user sees an error instead of a false success.
    If the mutation then fails, a summary has been marked stale unnecessarily,
    which costs one rebuild and leaks nothing. Conservative in the safe
    direction.
    """
    async with admin_session() as conn:
        cursor = await conn.execute(
            _STALE_ANY_ACTOR_SQL, {"source_id": source_id, "subject_id": subject_id}
        )
        stale = [dict(row) for row in await cursor.fetchall()]

        for row in stale:
            await conn.execute(
                """
                INSERT INTO audit_log (subject_id, actor_id, memory_id, action, metadata)
                VALUES (%s::uuid, %s::uuid, %s::uuid, 'update', %s::jsonb)
                """,
                (
                    subject_id,
                    actor_id,
                    str(row["id"]),
                    json.dumps(
                        {
                            "outcome": "stale",
                            "reason": reason,
                            "triggered_by": source_id,
                            "cross_actor": str(row["actor_id"]) != str(actor_id),
                        }
                    ),
                ),
            )

    if stale:
        log_event(
            "store.summary.stale",
            reason=reason,
            source_id=source_id,
            summaries=[str(row["id"]) for row in stale],
            cross_actor=[
                str(row["id"])
                for row in stale
                if str(row["actor_id"]) != str(actor_id)
            ],
        )

    return stale


async def mark_summary_stale(
    conn: Any,
    *,
    subject_id: str,
    actor_id: str,
    source_id: str,
    reason: str,
) -> list[dict[str, Any]]:
    """Invalidate the summary a changed memory was folded into.

    IN-TRANSACTION variant, scoped by the caller's RLS session. Use
    `invalidate_summaries_for` instead on any path where the summary might have
    been written by a different actor - notably erasure. See that function for
    the hole this one has.

    Returns the summaries it marked, so the caller can log them. Empty when the
    source was never consolidated, which is the common case.

    WHY THIS EXISTS. M7 promises erasure; M8 writes summaries; nothing joined
    them. A user deleted two preferences, both rows were soft-deleted correctly,
    and the summary quoting them stayed live and kept feeding the model the
    facts they had just erased. The delete APPEARED to work — the audit trail
    said so — and the fact reached the model anyway.

    The edge was already in the schema: `0007` gave every consolidated source a
    `consolidated_into` pointer and an index on it. Nothing had ever read it
    back. This walks it.

    Called for a delete, an edit and a supersession — anything that makes the
    source no longer support what the summary asserts. Runs inside the caller's
    transaction so the invalidation commits with the change that caused it; a
    summary left live after its source vanished is precisely the bug.

    `stale_at IS NULL` makes it idempotent: retiring three sources of one
    summary marks it once and leaves the original timestamp alone.
    """
    cursor = await conn.execute(
        _STALE_SQL, {"source_id": source_id, "subject_id": subject_id}
    )
    stale = [dict(row) for row in await cursor.fetchall()]

    for row in stale:
        await write_audit(
            conn,
            subject_id=subject_id,
            actor_id=actor_id,
            action=UPDATE,
            memory_id=str(row["id"]),
            metadata={
                "outcome": "stale",
                "reason": reason,
                "triggered_by": source_id,
            },
            allow_repeat=True,
        )

    if stale:
        log_event(
            "store.summary.stale",
            reason=reason,
            source_id=source_id,
            summaries=[str(row["id"]) for row in stale],
        )

    return stale


_SUPERSEDE_SQL = """
UPDATE memories
   SET superseded_by = %(new_id)s::uuid,
       superseded_at = now(),
       updated_at    = now()
 WHERE subject_id    = %(subject_id)s::uuid
   AND attribute     = %(attribute)s
   AND id           <> %(new_id)s::uuid
   AND deleted_at    IS NULL
   AND superseded_at IS NULL
RETURNING id, content
"""


async def supersede_slot(
    conn: Any,
    *,
    subject_id: str,
    actor_id: str,
    attribute: str,
    new_id: str,
) -> list[dict[str, Any]]:
    """Mark every other live occupant of a single-valued slot as superseded.

    Returns the rows it retired, so the caller can log and audit them.

    NOT A DELETE. `superseded_at` says the user changed their mind;
    `deleted_at` says they asked for erasure. Superseded rows leave retrieval —
    which is the whole point, since a preference the user has replaced must stop
    reaching the model — but stay in the curated list and the GDPR export,
    because "you said Java in September and C++ in October" is history they are
    entitled to see.

    `id <> new_id` guards the obvious own-goal: the row that just filled the
    slot must not supersede itself. `superseded_at IS NULL` makes the statement
    idempotent, so a retry cannot rewrite an earlier supersession's pointer and
    lose the chain.

    Called under the same advisory lock and inside the same transaction as the
    INSERT that triggered it, so a reader never sees two live values in a
    single-valued slot.
    """
    cursor = await conn.execute(
        _SUPERSEDE_SQL,
        {"new_id": new_id, "subject_id": subject_id, "attribute": attribute},
    )
    retired = [dict(row) for row in await cursor.fetchall()]

    for row in retired:
        # A superseded source no longer supports what a summary built on it
        # asserts, so the summary must be rebuilt. Same transaction as the
        # supersession itself.
        await mark_summary_stale(
            conn,
            subject_id=subject_id,
            actor_id=actor_id,
            source_id=str(row["id"]),
            reason="source_superseded",
        )

        # M7's trail: a supersession is a governed mutation of an existing row,
        # so it earns an audit entry like any other. `allow_repeat` because one
        # new value can retire several older ones in a single transaction —
        # rare, but it happens when a slot was somehow left with two occupants.
        await write_audit(
            conn,
            subject_id=subject_id,
            actor_id=actor_id,
            action=UPDATE,
            memory_id=str(row["id"]),
            metadata={
                "outcome": "superseded",
                "attribute": attribute,
                "superseded_by": new_id,
            },
            allow_repeat=True,
        )

    return retired


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
            attribute = getattr(candidate, "attribute", None)
            source = getattr(candidate, "source", None)

            # ---- M9 item 4: STRICT PROVENANCE ------------------------------
            #
            # Capture reads BOTH halves of a turn, user and assistant alike,
            # so the extractor can mint a memory out of the assistant's own
            # words. That is useful for genuine inferences ("the user has travel
            # experience") and dangerous for anything else, because the
            # assistant's reply is downstream of what retrieval fed it. The
            # observed failure: a summary that had not yet been invalidated told
            # the model the user preferred Java, the model said so, and capture
            # stored "The user prefers Java." as a brand-new live row ninety
            # seconds after the user had deleted exactly that.
            #
            # Rule 1: the assistant may not SET A SLOT. This is new risk that
            # supersession created — without it, an assistant_note could
            # supersede the user's real preference, and the system would
            # overwrite what the user said with what it had just said itself.
            assistant_sourced = source == "assistant_note"
            if assistant_sourced and attribute is not None:
                log_event(
                    "capture.persist.assistant_slot_stripped",
                    attribute=attribute,
                    text=text[:80],
                )
                attribute = None

            single_valued = is_single_valued(attribute)

            # Rule 2: the assistant may not RESURRECT what the user removed.
            #
            # A user restating a fact means they still hold it, so it should
            # come back — no tombstone check on their side. The assistant
            # restating one means only that it read it somewhere, quite possibly
            # from a stale summary. Skipping the candidate entirely (no row, no
            # audit) is right: nothing happened, and recording that nothing
            # happened would just be noise in the trail.
            if assistant_sourced and embedding:
                tombstones = await find_similar_tombstones(
                    conn, subject_id, embedding, limit=1
                )
                if (
                    tombstones
                    and tombstones[0]["similarity"] is not None
                    and tombstones[0]["similarity"] >= limit
                ):
                    grave = tombstones[0]
                    log_event(
                        "capture.persist.resurrection_blocked",
                        similarity=float(grave["similarity"]),
                        threshold=limit,
                        blocked=text[:80],
                        matched=str(grave["content"])[:80],
                        reason=(
                            "deleted" if grave["deleted_at"] is not None else "superseded"
                        ),
                    )
                    results.append(
                        {
                            "text": text,
                            "action": "blocked",
                            "memory_id": None,
                            "similarity": float(grave["similarity"]),
                            "reason": "assistant_resurrection",
                            "dedup_status_at_write": None,
                            "dedup_status_from_node": getattr(
                                candidate, "dedup_status", None
                            ),
                        }
                    )
                    continue

            match: dict[str, Any] | None = None
            if embedding:
                rows = await find_similar(conn, subject_id, embedding, limit=1)
                if rows and rows[0]["similarity"] is not None and rows[0]["similarity"] >= limit:
                    match = rows[0]

            # DEDUP MUST NOT SWALLOW A CHANGE OF MIND.
            #
            # Two statements filling the same single-valued slot with different
            # values are a supersession, however similar they read. Left to the
            # cosine threshold alone, the newer statement would "reinforce" the
            # older row — the count would rise, the CONTENT would stay the old
            # value, and the user's update would vanish silently.
            #
            # Measured on the real rows that produced this bug:
            #
            #   "The user prefers c++."  vs  "The user prefers Java."   0.7735
            #   "The user prefers c++."  vs  "…code in Python."         0.7401
            #
            # Both sit below the 0.82 threshold, so today they would not have
            # collided. That is luck, not design — "Java" vs "Kotlin" is a
            # closer pair, and the margin is 0.05. This guard removes the
            # dependence on the margin entirely.
            if (
                match is not None
                and single_valued
                and match.get("attribute") == attribute
                and str(match.get("content", "")).strip() != text.strip()
            ):
                log_event(
                    "capture.persist.supersedes_near_duplicate",
                    attribute=attribute,
                    similarity=float(match["similarity"]),
                    threshold=limit,
                    existing=str(match.get("content", ""))[:80],
                    incoming=text[:80],
                )
                match = None

            if match is not None and assistant_sourced:
                # Rule 3: the assistant repeating itself is not evidence.
                #
                # `reinforcement_count` means "the user said this again", and it
                # feeds M4's frequency signal. Letting the assistant increment
                # it created a loop with a clear direction: retrieval surfaces a
                # fact, the model restates it, capture reinforces it, its
                # frequency score rises, retrieval surfaces it more. Repetition
                # was outcompeting recency, and the facts that won were the ones
                # the system had been talking to itself about.
                #
                # M9 also halved the frequency weight (0.2 -> 0.1) for the same
                # reason; this closes the loop at the source rather than merely
                # damping it.
                log_event(
                    "capture.persist.assistant_reinforcement_skipped",
                    similarity=float(match["similarity"]),
                    memory_id=str(match["id"]),
                    text=text[:80],
                )
                results.append(
                    {
                        "text": text,
                        "action": "ignored",
                        "memory_id": str(match["id"]),
                        "similarity": float(match["similarity"]),
                        "reason": "assistant_note_duplicate",
                        "dedup_status_at_write": "duplicate",
                        "dedup_status_from_node": getattr(candidate, "dedup_status", None),
                    }
                )
            elif match is not None:
                await reinforce(str(match["id"]), conn=conn)
                # M7 step 3. Inside the same transaction and the same advisory
                # lock as the reinforcement itself, so the row and its audit
                # entry commit or roll back together.
                await write_audit(
                    conn,
                    subject_id=subject_id,
                    actor_id=actor_id,
                    action=WRITE,
                    memory_id=str(match["id"]),
                    metadata={
                        "outcome": "reinforce",
                        "similarity": float(match["similarity"]),
                        "source": getattr(candidate, "source", None),
                    },
                    # This candidate may be reinforcing a row that an EARLIER
                    # candidate in this same batch just inserted (or reinforced)
                    # -- that is the whole point of intra-turn dedup. Each is a
                    # separate governed action and earns its own audit row; the
                    # guard would otherwise collapse them and under-report the
                    # trail. See store/audit.py on why the key is still recorded.
                    allow_repeat=True,
                )
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
                    attribute=attribute,
                    conn=conn,
                )

                # M9: a new value in a single-valued slot retires the old one,
                # inside this same transaction and advisory lock so no reader
                # ever sees two live values for one slot.
                superseded: list[dict[str, Any]] = []
                if single_valued:
                    superseded = await supersede_slot(
                        conn,
                        subject_id=subject_id,
                        actor_id=actor_id,
                        attribute=str(attribute),
                        new_id=memory_id,
                    )
                    if superseded:
                        log_event(
                            "capture.persist.superseded",
                            attribute=attribute,
                            new_id=memory_id,
                            retired=[str(row["id"]) for row in superseded],
                            count=len(superseded),
                        )
                # M7 step 3, same transaction as the INSERT above. Note the
                # ordering matters for more than atomicity: `audit_log.memory_id`
                # has a foreign key to `memories(id)`, so the audit row can only
                # be written after the memory row exists.
                await write_audit(
                    conn,
                    subject_id=subject_id,
                    actor_id=actor_id,
                    action=WRITE,
                    memory_id=memory_id,
                    metadata={
                        "outcome": "insert",
                        "source": getattr(candidate, "source", None),
                        "importance": getattr(candidate, "importance", None),
                        "confidence": getattr(candidate, "confidence", None),
                    },
                    # An insert always targets a brand-new id, so this cannot
                    # collide today. Flagged anyway for symmetry with the
                    # reinforce branch: both are per-candidate actions in a loop,
                    # and the reason they are exempt is the loop, not the outcome.
                    allow_repeat=True,
                )
                results.append(
                    {
                        "text": text,
                        "action": "insert",
                        "memory_id": memory_id,
                        "attribute": attribute,
                        "superseded": [str(row["id"]) for row in superseded],
                        "similarity": getattr(candidate, "similarity", None),
                        "dedup_status_at_write": "new",
                        "dedup_status_from_node": getattr(candidate, "dedup_status", None),
                    }
                )

    return results
