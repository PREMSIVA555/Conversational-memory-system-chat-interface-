"""GDPR export (M7 steps 9, 10, 11).

    GET /memories/export   everything stored about the caller, as one JSON dump

THE EXPORT IS NOT THE CURATED VIEW, AND MUST NOT BE
---------------------------------------------------
`GET /memories/me` answers "what does the assistant currently remember about
me?". This endpoint answers a different, legally distinct question: "what do you
hold on me?" — which includes the records the curated view hides. Concretely it
is a **strict superset** (step 10):

  * every `memories` row for the subject, soft-deleted ones included, each
    carrying an explicit `deleted: true|false` alongside its `deleted_at`. A
    deletion is itself a fact held about the user; suppressing it would make the
    export a prettier lie than the curated view.
  * every `audit_log` row for the subject — the record of what was written,
    read, updated, deleted and exported, and when.
  * every `feedback` row for the subject.

The `deleted` boolean is redundant with `deleted_at != null` and is sent anyway,
because step 10 asks for the deletion to be *flagged* rather than inferrable: a
consumer reading the JSON should not have to know that a timestamp doubles as a
tombstone.

A NOTE ON `feedback` AND ITS RLS POLICY
---------------------------------------
`feedback` has no `actor_id` column (M1 plan step 11 defines it that way), so
0005's policies scope it on `subject_id = app.subject_id` plus
`app.actor_id IS NOT NULL` — presence, not equality — while `memories` and
`audit_log` scope both columns by equality. For this endpoint that asymmetry is
harmless and worth stating plainly rather than working around: the subject
predicate is what carries the auth boundary, the export is scoped by subject,
and every query below also states `WHERE subject_id = %s` in the SQL, so the
result is identical whether RLS is doing anything or not. Where it *would*
matter is a future multi-actor model, in which two actors of the same subject
could read each other's feedback; that is a schema change (add `actor_id` to
`feedback`), not something this endpoint can paper over, and it is reported
rather than silently handled.

WHY THE EXPORT'S OWN AUDIT ROW IS WRITTEN LAST, IN THE SAME TRANSACTION
-----------------------------------------------------------------------
Step 11 wants one `export` audit row carrying the row counts. Writing it after
the SELECTs and before COMMIT means: the counts in `metadata` are exactly the
counts in the payload the caller received; the export row does not appear in the
`audit_log` array it is describing (it does not exist yet when that array is
read); and if the transaction rolls back, no export is logged that no one
received.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query

from api.memories import (
    MEMORY_COLUMNS,
    Identity,
    resolve_identity,
    serialize_memory,
)
from store.audit import EXPORT, write_audit
from store.db import session

logger = logging.getLogger("memsys.api.governance")

router = APIRouter(tags=["governance"])

__all__ = ["router", "export_my_data"]

#: Hard ceiling on rows per table in one export. A GDPR export is a dump, not a
#: paginated view, so the default is deliberately huge — but unbounded is not a
#: default anyone should ship: a subject with a runaway audit trail would
#: otherwise turn one request into an out-of-memory event. `truncated` in the
#: response says plainly when a cap was hit, rather than silently short-changing
#: a legal request.
DEFAULT_ROW_CAP = 100_000


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _serialize_audit(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "subject_id": str(row["subject_id"]),
        "actor_id": str(row["actor_id"]),
        "memory_id": str(row["memory_id"]) if row.get("memory_id") else None,
        "action": row["action"],
        "metadata": row.get("metadata") or {},
        "created_at": _iso(row.get("created_at")),
    }


def _serialize_feedback(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "subject_id": str(row["subject_id"]),
        "memory_id": str(row["memory_id"]) if row.get("memory_id") else None,
        "signal": row.get("signal"),
        "comment": row.get("comment"),
        "created_at": _iso(row.get("created_at")),
    }


@router.get("/memories/export")
async def export_my_data(
    identity: Identity = Depends(resolve_identity),
    row_cap: int = Query(DEFAULT_ROW_CAP, ge=1, description="Max rows per table."),
) -> dict[str, Any]:
    """Everything stored on the caller: memories (deleted included), audit, feedback.

    Everything happens in ONE transaction — the three SELECTs and the `export`
    audit row. A dump assembled from three separate transactions could catch a
    memory in one and miss its audit row in another, and then claim in its own
    `counts` metadata to be a consistent snapshot.
    """
    async with session(identity.subject_id, identity.actor_id) as conn:
        # No `deleted_at IS NULL` here, and that omission is the entire point of
        # the endpoint. Ordered ascending so the dump reads as a history.
        cursor = await conn.execute(
            f"""
            SELECT {MEMORY_COLUMNS}
              FROM memories
             WHERE subject_id = %s
             ORDER BY created_at ASC, id
             LIMIT %s
            """,
            (identity.subject_id, row_cap),
        )
        memory_rows = [dict(row) for row in await cursor.fetchall()]

        cursor = await conn.execute(
            """
            SELECT id, subject_id, actor_id, memory_id, action, metadata, created_at
              FROM audit_log
             WHERE subject_id = %s
             ORDER BY created_at ASC, id
             LIMIT %s
            """,
            (identity.subject_id, row_cap),
        )
        audit_rows = [dict(row) for row in await cursor.fetchall()]

        cursor = await conn.execute(
            """
            SELECT id, subject_id, memory_id, signal, comment, created_at
              FROM feedback
             WHERE subject_id = %s
             ORDER BY created_at ASC, id
             LIMIT %s
            """,
            (identity.subject_id, row_cap),
        )
        feedback_rows = [dict(row) for row in await cursor.fetchall()]

        memories = [serialize_memory(row, include_deleted_marker=True) for row in memory_rows]
        counts = {
            "memories": len(memories),
            "memories_deleted": sum(1 for m in memories if m["deleted"]),
            "memories_live": sum(1 for m in memories if not m["deleted"]),
            "audit_log": len(audit_rows),
            "feedback": len(feedback_rows),
        }

        # Step 11 — the export is itself an auditable event. Written last so the
        # counts describe the payload actually returned, and so this row is not
        # inside the `audit_log` array it is reporting on.
        audit_id = await write_audit(
            conn,
            subject_id=identity.subject_id,
            actor_id=identity.actor_id,
            action=EXPORT,
            memory_id=None,
            metadata={"counts": counts, "row_cap": row_cap},
        )

    truncated = {
        table: True
        for table, n in (
            ("memories", len(memories)),
            ("audit_log", len(audit_rows)),
            ("feedback", len(feedback_rows)),
        )
        if n >= row_cap
    }
    if truncated:
        logger.warning(
            "GDPR export hit the row cap for subject=%s tables=%s",
            identity.subject_id,
            sorted(truncated),
        )

    return {
        "subject_id": identity.subject_id,
        "actor_id": identity.actor_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        # A GDPR dump has to be re-readable by whoever receives it, so it says
        # what it contains rather than assuming the reader has the source.
        "schema": {
            "memories": "every memory ever stored for this subject, including "
                        "soft-deleted rows (deleted=true, deleted_at set)",
            "audit_log": "append-only record of every write/read/update/delete/export",
            "feedback": "per-memory signal this subject submitted",
        },
        "memories": memories,
        "audit_log": [_serialize_audit(row) for row in audit_rows],
        "feedback": [_serialize_feedback(row) for row in feedback_rows],
        "truncated": truncated,
        "export_audit_id": audit_id,
    }
