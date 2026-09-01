"""Memory governance endpoints (M7 steps 5, 6, 7, 12).

    GET    /memories/me          the curated view — live memories, newest first
    DELETE /memories/{id}        soft delete: sets `deleted_at`, never removes
    PATCH  /memories/{id}        edit content, re-embed, audit

`GET /memories/export` lives in `api/governance.py`: it is a different thing
with different rules (it deliberately includes what this module hides), and
keeping the curated view and the full dump in separate modules makes it much
harder to "fix" one by accidentally teaching it the other's behaviour.

IDENTITY, AND WHY IT IS A QUERY PARAMETER
-----------------------------------------
There is no authentication layer in this system yet — `POST /chat` takes
`subject_id` in its body for exactly the same reason. So the caller states who
they are, and every endpoint here scopes to that identity in three independent
ways (step 12):

  1. an explicit application-level ownership check (`ensure_owned`), which
     compares the row's `subject_id` to the caller's and raises 403 on a
     mismatch, 404 on a missing row;
  2. an explicit `AND subject_id = %s` predicate in every statement;
  3. M1's row-level security, via the `app.subject_id` / `app.actor_id` GUCs
     that `store.db.session()` sets.

That looks redundant and is not. Layer 3 alone is enough to make the *tests*
pass — a verifier on this project demonstrated that deleting the app-level
predicate from `retrieve/semantic.py` changed no test result, because RLS was
quietly doing the work — which is precisely why layer 3 alone is not enough to
*trust*. `ensure_owned` is deliberately written to take a connection so it can
be exercised on an admin connection, where RLS is not there to rescue it; see
`test_cannot_delete_another_subjects_memory`.

Layer 3 also changes the *status code*, not just the outcome: under RLS another
subject's row is invisible, so the honest answer is 404 (we cannot confirm it
exists), and 403 is reachable only when the check runs somewhere RLS is not
filtering. Both are acceptable answers to "you may not touch this".

WHAT IS AUDITED HERE, AND WHAT IS NOT
-------------------------------------
`DELETE` and `PATCH` each write exactly one audit row, in the transaction that
performs the change.

`GET /memories/me` writes **none**, and that is a deliberate gap rather than an
oversight. Plan step 4 scopes read auditing to the retrieval path — memories
that reach the *model's* prompt — and this endpoint discloses memories to the
subject themselves, who is the one the trail exists to protect. The argument for
auditing it anyway is real (it is still a disclosure, and a future multi-actor
model would make "who listed whose memories" worth knowing); the argument
against is that a curated-view read is self-service and auditing it would emit a
row per memory per page-load, swamping the trail with the least interesting
event in it.

`GET /memories/export` is treated differently and does log — one `export` row
carrying the row counts — because an export is a bulk extraction of everything
held, which is exactly the event a GDPR trail should record.

Recorded here rather than silently: if M6 or M8 decides the curated view should
audit, the hook belongs in `list_my_memories` and nowhere else.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Response
from pydantic import BaseModel, Field

from store.audit import DELETE as AUDIT_DELETE
from store.audit import UPDATE as AUDIT_UPDATE
from store.audit import write_audit
from store.db import load_env, session

logger = logging.getLogger("memsys.api.memories")

router = APIRouter(tags=["memories"])

__all__ = [
    "router",
    "Identity",
    "resolve_identity",
    "ensure_owned",
    "MEMORY_COLUMNS",
    "serialize_memory",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
]

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500

#: `lock_timeout` for the per-memory advisory lock. Bounded for the same reason
#: `store/memories.py` bounds its own: the lock is held across two short
#: statements and never across a provider call, so a legitimate wait is
#: milliseconds and an unbounded one would turn a wedged transaction elsewhere
#: into a request that hangs forever.
LOCK_TIMEOUT_MS = 5000

# hashtext() returns int4, which is what the single-argument advisory lock
# functions take. Keyed on the memory id (not the subject) because these
# endpoints contend per row, and locking the whole subject would serialise a
# bulk delete against unrelated captures.
_MEMORY_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext(%s))"
_LOCK_TIMEOUT_SQL = "SELECT set_config('lock_timeout', %s, true)"

#: Columns the curated view exposes. `embedding` is deliberately absent — a
#: 1024-float vector per row is megabytes of payload the UI has no use for, and
#: `content_tsv` is a derived index artefact.
MEMORY_COLUMNS = """
    id, subject_id, actor_id, content, source, importance, confidence,
    weight, reinforcement_count, created_at, updated_at, last_accessed_at,
    deleted_at
"""


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

class Identity:
    """The caller's `(subject_id, actor_id)` pair, both validated as uuids."""

    __slots__ = ("subject_id", "actor_id")

    def __init__(self, subject_id: str, actor_id: str) -> None:
        self.subject_id = subject_id
        self.actor_id = actor_id

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Identity(subject_id={self.subject_id!r}, actor_id={self.actor_id!r})"


def _as_uuid(value: str, field: str) -> str:
    """Validate and normalise a uuid, or 400.

    Without this a malformed id reaches a `::uuid` cast and comes back as a
    psycopg `InvalidTextRepresentation`, i.e. a 500 — the server blaming itself
    for the client's typo.
    """
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail=f"{field} is not a valid uuid") from None


def resolve_identity(
    subject_id: Optional[str] = Query(None, description="Whose memories. Defaults to the X-Subject-Id header."),
    actor_id: Optional[str] = Query(None, description="Who is asking. Defaults to subject_id."),
    x_subject_id: Optional[str] = Header(None, alias="X-Subject-Id"),
    x_actor_id: Optional[str] = Header(None, alias="X-Actor-Id"),
) -> Identity:
    """Work out who the caller is, from query params, headers, or the environment.

    Precedence: query parameter, then header, then `DEFAULT_SUBJECT_ID` in
    `infra/.env`. The environment fallback exists so the milestone's manual
    verification (`curl -s localhost:8000/memories/me | jq 'length'`) is
    runnable at all — it names no subject, and without a default there is no
    caller to be. It is a development convenience: unset, which is the shipped
    state, every endpoint here demands an explicit identity and answers 400
    without one. It is not, and must not be mistaken for, authentication.
    """
    load_env()
    resolved_subject = subject_id or x_subject_id or os.environ.get("DEFAULT_SUBJECT_ID")
    if not resolved_subject:
        raise HTTPException(
            status_code=400,
            detail=(
                "subject_id is required (query parameter, X-Subject-Id header, "
                "or DEFAULT_SUBJECT_ID in the environment)"
            ),
        )
    subject = _as_uuid(resolved_subject, "subject_id")
    # Single-user mode: the actor is the subject (M1's schema seam). Both GUCs
    # are set regardless, because every `memories` RLS policy tests both.
    actor_raw = actor_id or x_actor_id or subject
    return Identity(subject, _as_uuid(actor_raw, "actor_id"))


# ---------------------------------------------------------------------------
# step 12 — the application-level ownership check
# ---------------------------------------------------------------------------

async def ensure_owned(
    conn: Any,
    memory_id: str,
    subject_id: str,
    *,
    require_live: bool = True,
) -> dict[str, Any]:
    """Load a memory and prove the caller owns it, or raise 403/404.

    Deliberately takes `conn` rather than opening its own session, for two
    reasons that are both load-bearing:

      * it runs inside the caller's transaction, under the same advisory lock,
        so the row it validates is the row that is about to be mutated — not a
        snapshot from before someone else's commit;
      * it can be called on a connection where RLS is *not* filtering (the
        owner connection from `store.db.admin_session()`), which is the only way
        to prove this check does any work of its own. Called on an ordinary
        app-role session, another subject's row is already invisible and this
        function would return 404 whether or not its subject comparison
        existed. Every test of an auth boundary on this project has to answer
        "would it still fail if I deleted the check?", and this signature is the
        answer.

    Note the SELECT has no `subject_id` predicate on purpose — the comparison is
    made in Python, explicitly, so it is visible and testable rather than folded
    into a WHERE clause that RLS would duplicate.
    """
    cursor = await conn.execute(
        f"SELECT {MEMORY_COLUMNS} FROM memories WHERE id = %s", (memory_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        # Either it never existed, or RLS is hiding another subject's row. Both
        # answer 404: confirming existence would itself leak.
        raise HTTPException(status_code=404, detail="memory not found")

    row = dict(row)
    if str(row["subject_id"]) != str(subject_id):
        raise HTTPException(status_code=403, detail="memory belongs to another subject")

    if require_live and row.get("deleted_at") is not None:
        # Already soft-deleted. 404 rather than 410 or 204: to this API the row
        # is gone, and a second DELETE must not write a second audit row.
        raise HTTPException(status_code=404, detail="memory not found")

    return row


# ---------------------------------------------------------------------------
# serialisation
# ---------------------------------------------------------------------------

def serialize_memory(row: dict[str, Any], *, include_deleted_marker: bool = False) -> dict[str, Any]:
    """One `memories` row as JSON-safe primitives.

    `include_deleted_marker` adds the explicit `deleted` boolean the GDPR export
    needs (step 10). The curated view leaves it off — it only ever returns live
    rows, and a `deleted: false` on every one of them would invite a consumer to
    start filtering on a field that is constant there.
    """
    def _iso(value: Any) -> Optional[str]:
        return value.isoformat() if value is not None else None

    payload: dict[str, Any] = {
        "id": str(row["id"]),
        "subject_id": str(row["subject_id"]),
        "actor_id": str(row["actor_id"]),
        "content": row["content"],
        "source": row.get("source"),
        "importance": row.get("importance"),
        "confidence": row.get("confidence"),
        "weight": row.get("weight"),
        "reinforcement_count": row.get("reinforcement_count"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "last_accessed_at": _iso(row.get("last_accessed_at")),
    }
    if include_deleted_marker:
        payload["deleted"] = row.get("deleted_at") is not None
        payload["deleted_at"] = _iso(row.get("deleted_at"))
    return payload


# ---------------------------------------------------------------------------
# step 6 — GET /memories/me
# ---------------------------------------------------------------------------

@router.get("/memories/me")
async def list_my_memories(
    response: Response,
    identity: Identity = Depends(resolve_identity),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    """The curated view: live memories for the caller, newest first.

    Returns a **bare JSON array**, not an envelope, so the milestone's
    `curl … /memories/me | jq 'length'` means what it looks like it means.
    Pagination metadata rides in `X-Total-Count` / `X-Limit` / `X-Offset`
    headers instead, which keeps the body's shape stable as the page moves.

    `deleted_at IS NULL` is stated here as well as in the retrieval paths
    (step 8) — this query is a third read path, and a curated view that leaked
    deleted rows would be the most visible possible failure of the milestone.
    """
    async with session(identity.subject_id, identity.actor_id) as conn:
        cursor = await conn.execute(
            "SELECT count(*) AS n FROM memories WHERE subject_id = %s AND deleted_at IS NULL",
            (identity.subject_id,),
        )
        total = int((await cursor.fetchone())["n"])

        cursor = await conn.execute(
            f"""
            SELECT {MEMORY_COLUMNS}
              FROM memories
             WHERE subject_id = %s
               AND deleted_at IS NULL
             ORDER BY created_at DESC, id
             LIMIT %s OFFSET %s
            """,
            (identity.subject_id, limit, offset),
        )
        rows = [dict(row) for row in await cursor.fetchall()]

    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    return [serialize_memory(row) for row in rows]


# ---------------------------------------------------------------------------
# step 5 — DELETE /memories/{id}
# ---------------------------------------------------------------------------

@router.delete("/memories/{memory_id}")
async def delete_memory(
    memory_id: str = Path(..., description="The memory to soft-delete."),
    identity: Identity = Depends(resolve_identity),
) -> dict[str, Any]:
    """Soft-delete one memory and write exactly one `delete` audit row.

    NEVER A HARD DELETE. The row stays, `deleted_at` is stamped, and every read
    path filters it out (steps 6 and 8). The GDPR export still returns it, with
    the deletion marked — a user is entitled to see that a deletion happened.

    CONCURRENCY — `test_concurrent_deletes_write_single_audit_row`
    -------------------------------------------------------------
    Two simultaneous deletes of the same id must yield one 200, one 404, and
    exactly one audit row. Two things make that hold:

      * `pg_advisory_xact_lock(hashtext(id))` serialises the two transactions,
        released automatically at COMMIT or ROLLBACK — the same mechanism M2
        uses in `store/memories.py`, and chosen for the same reason: there is no
        unlock bookkeeping to get wrong, including on a dropped connection.
      * the UPDATE carries `AND deleted_at IS NULL`. The second transaction
        wakes up after the first commits, takes a fresh statement snapshot
        (READ COMMITTED), sees the row already deleted, matches zero rows, and
        returns 404 *before* reaching `write_audit`.

    Either one alone would very nearly work; together the outcome is
    deterministic rather than nearly. The audit row is written on the same
    connection inside the same transaction, so a rollback after the UPDATE
    cannot leave a delete logged that did not happen.
    """
    memory_id = _as_uuid(memory_id, "memory_id")

    async with session(identity.subject_id, identity.actor_id) as conn:
        await conn.execute(_LOCK_TIMEOUT_SQL, (f"{LOCK_TIMEOUT_MS}ms",))
        await conn.execute(_MEMORY_LOCK_SQL, (memory_id,))

        # Layer 1: the explicit application-level ownership check (step 12).
        await ensure_owned(conn, memory_id, identity.subject_id)

        # Layer 2: the same scoping restated in SQL, plus the liveness predicate
        # that decides the concurrent race.
        cursor = await conn.execute(
            """
            UPDATE memories
               SET deleted_at = now(),
                   updated_at = now()
             WHERE id = %s
               AND subject_id = %s
               AND deleted_at IS NULL
            RETURNING id, deleted_at
            """,
            (memory_id, identity.subject_id),
        )
        row = await cursor.fetchone()
        if row is None:
            # Lost the race, or RLS filtered it. No audit row: nothing happened.
            raise HTTPException(status_code=404, detail="memory not found")

        audit_id = await write_audit(
            conn,
            subject_id=identity.subject_id,
            actor_id=identity.actor_id,
            action=AUDIT_DELETE,
            memory_id=memory_id,
            metadata={"soft": True},
        )

    logger.info("memory soft-deleted id=%s subject=%s", memory_id, identity.subject_id)
    return {
        "id": memory_id,
        "deleted": True,
        "deleted_at": row["deleted_at"].isoformat(),
        "audit_id": audit_id,
    }


# ---------------------------------------------------------------------------
# step 7 — PATCH /memories/{id}
# ---------------------------------------------------------------------------

class MemoryPatch(BaseModel):
    content: str = Field(..., min_length=1, description="Replacement text for the memory.")


@router.patch("/memories/{memory_id}")
async def patch_memory(
    patch: MemoryPatch,
    memory_id: str = Path(..., description="The memory to edit."),
    identity: Identity = Depends(resolve_identity),
) -> dict[str, Any]:
    """Replace a memory's content, re-embed it, and write one `update` audit row.

    WHY THE EMBEDDING CALL IS OUTSIDE THE WRITE TRANSACTION
    -------------------------------------------------------
    Re-embedding is a network round-trip to Voyage, and on this project's tier
    it can sit out a rate-limit window measured in *tens of seconds*. Holding an
    advisory lock and an open transaction across that would block every other
    write to the row for the duration, for no benefit. So the sequence is:

        session A:  ownership check (cheap, aborts before any provider spend)
        no session: embed the new content
        session B:  advisory lock -> re-check ownership -> UPDATE -> audit

    Ownership is checked twice on purpose: once to avoid paying for an embedding
    the caller was never allowed to request, and once inside the transaction
    that actually writes, where it is the check that counts.

    A stale content column with a fresh embedding, or the reverse, would poison
    the semantic path silently — so both columns are written in one statement,
    and the audit row shares its transaction.
    """
    memory_id = _as_uuid(memory_id, "memory_id")
    content = patch.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="content must not be blank")

    async with session(identity.subject_id, identity.actor_id) as conn:
        before = await ensure_owned(conn, memory_id, identity.subject_id)

    # The LLM seam. `llm/config.py` is the only module that names a model;
    # imported here rather than at module scope so importing this router does
    # not drag litellm in.
    from llm.config import embed

    vectors = await embed([content])
    if not vectors or not vectors[0]:
        # A memory whose embedding silently became NULL would vanish from the
        # semantic path while still looking present in the curated view. Refuse
        # the edit instead.
        raise HTTPException(status_code=502, detail="embedding provider returned no vector")
    literal = "[" + ",".join(repr(float(v)) for v in vectors[0]) + "]"

    async with session(identity.subject_id, identity.actor_id) as conn:
        await conn.execute(_LOCK_TIMEOUT_SQL, (f"{LOCK_TIMEOUT_MS}ms",))
        await conn.execute(_MEMORY_LOCK_SQL, (memory_id,))

        await ensure_owned(conn, memory_id, identity.subject_id)

        cursor = await conn.execute(
            f"""
            UPDATE memories
               SET content    = %s,
                   embedding  = %s::vector,
                   updated_at = now()
             WHERE id = %s
               AND subject_id = %s
               AND deleted_at IS NULL
            RETURNING {MEMORY_COLUMNS}
            """,
            (content, literal, memory_id, identity.subject_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="memory not found")

        audit_id = await write_audit(
            conn,
            subject_id=identity.subject_id,
            actor_id=identity.actor_id,
            action=AUDIT_UPDATE,
            memory_id=memory_id,
            metadata={
                "content_chars_before": len(before["content"] or ""),
                "content_chars_after": len(content),
                "reembedded": True,
                "embedding_dim": len(vectors[0]),
            },
        )

    payload = serialize_memory(dict(row))
    payload["audit_id"] = audit_id
    return payload
