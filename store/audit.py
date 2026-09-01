"""The audit trail (M7 steps 1, 13, 14): one row per governed action.

`audit_log` is append-only at the database level — see
``store/migrations/0006_audit_append_only.sql``. This module is the only place
that writes to it.

THE TRANSACTION RULE (step 13)
------------------------------
``write_audit()`` **requires** a live connection; it will not open one for you.
That is the whole design, not an inconvenience:

    the audit row must commit or roll back with the action it describes.

If this function opened its own session, a delete that succeeded and an audit
write that failed would leave the trail silently short by one row, and a delete
that rolled back after its audit row committed would leave the trail claiming
something that never happened. Passing the caller's connection makes both
impossible — one transaction, two statements, one fate.

The one deliberate exception is `record_read_audit()`, which retrieval calls
after its own read transactions have already closed; see its docstring.

THE DOUBLE-WRITE GUARD (step 13)
--------------------------------
"Exactly one audit row per action — not zero, not two" is the property that
matters, and it is easy to break by accident: hook the write path in both
`capture/write.py` and `store/memories.py` and every capture writes two rows.
Structure is the primary defence (each action emits from exactly one call
site), but structure is a convention a future edit can violate silently.

`write_audit()` therefore also enforces it at runtime: within one database
transaction, a second attempt to log the same `(action, memory_id)` is dropped
and logged as a warning rather than inserted. The key is the transaction id
from `txid_current()`, so the guard is scoped exactly as tightly as the
transaction whose atomicity it is protecting, and a pooled connection reused by
a later transaction starts clean.

A REPEAT IS NOT ALWAYS A DUPLICATE — `allow_repeat`
---------------------------------------------------
`(action, memory_id)` alone is too blunt, and keying on it unconditionally
caused a real under-count. `persist_candidates()` can perform *two genuine
governed actions on the same row inside one transaction*: candidate A inserts a
memory, and candidate B — a restatement of A — dedups onto that same row and
reinforces it. Two actions, and the guard silently collapsed them into one audit
row, so the trail under-reported what happened.

Nothing inside this function can tell that case apart from an accidental double
hook: both look like "the same action on the same memory, twice, in one
transaction". So the caller states its intent. `allow_repeat=True` means "I know
this is a further, distinct action on the same row" and skips the suppression.

Crucially it still *records* the key. A stray second emission from somewhere
else — the exact `capture/write.py` double-hook this guard exists to catch —
does not pass the flag, finds the key already present, and is still suppressed.
So the protection survives while the legitimate repeat gets its row.

Note what the guard does NOT do, on purpose: it does not deduplicate across
transactions. Two concurrent deletes of the same memory are two transactions,
and the reason only one audit row results is that only one of them successfully
flips `deleted_at` — the losing transaction never reaches `write_audit()` at
all. Serializing that is the caller's job (`api/memories.py` takes
`pg_advisory_xact_lock` on the memory id, the same mechanism M2 uses in
`store/memories.py`), not this module's.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger("memsys.audit")

__all__ = [
    "ACTIONS",
    "WRITE",
    "READ",
    "DELETE",
    "UPDATE",
    "EXPORT",
    "AUDIT_ROWS",
    "AUDIT_DUPLICATES",
    "UnknownAuditAction",
    "write_audit",
    "write_audit_many",
    "record_read_audit",
    "count_actions",
]

WRITE = "write"
READ = "read"
DELETE = "delete"
UPDATE = "update"
EXPORT = "export"

#: The closed set from plan step 1. `action` is a bare `text` column (0005 has
#: no CHECK constraint), so this is where a typo like "deleted" is caught —
#: loudly, at the call site, rather than becoming an orphan value nobody
#: queries for.
ACTIONS: frozenset[str] = frozenset({WRITE, READ, DELETE, UPDATE, EXPORT})


class UnknownAuditAction(ValueError):
    """Raised for an `action` outside `ACTIONS`."""


# ---------------------------------------------------------------------------
# metrics (step 14)
# ---------------------------------------------------------------------------

def _get_or_create(factory, name: str):
    """Build a collector, tolerating a double import of this module.

    Same problem `retrieve/breaker.py` documents: pytest can import a module
    under two names in one process and the default Prometheus registry raises on
    a duplicate collector. Reimplemented locally in three lines rather than
    imported, so `store/` does not grow a dependency on `retrieve/`.
    """
    try:
        return factory()
    except ValueError:
        from prometheus_client import REGISTRY

        existing = REGISTRY._names_to_collectors.get(name)
        if existing is None:  # pragma: no cover - a collision implies presence
            raise
        return existing


def _build_audit_counter():
    from prometheus_client import Counter

    return Counter(
        "memsys_audit_rows_total",
        "Audit log rows written, by action",
        ["action"],
    )


def _build_duplicate_counter():
    from prometheus_client import Counter

    return Counter(
        "memsys_audit_duplicate_suppressed_total",
        "Audit writes dropped by the same-transaction double-write guard",
        ["action"],
    )


#: Step 14. One counter, labelled by action, rather than five counters — a
#: Prometheus label is exactly the right shape for a closed enum and keeps
#: `sum(rate(memsys_audit_rows_total[5m])) by (action)` a one-liner.
AUDIT_ROWS = _get_or_create(_build_audit_counter, "memsys_audit_rows_total")
AUDIT_DUPLICATES = _get_or_create(
    _build_duplicate_counter, "memsys_audit_duplicate_suppressed_total"
)


# ---------------------------------------------------------------------------
# the same-transaction double-write guard
# ---------------------------------------------------------------------------

#: Attribute name for the per-connection guard state. Stashed on the connection
#: object because a psycopg connection is used by exactly one transaction at a
#: time, so it is the natural scope — and it disappears with the connection
#: rather than accumulating in a module-level dict that would leak.
_GUARD_ATTR = "_memsys_audit_guard"


async def _transaction_key(conn: Any) -> Optional[str]:
    """The current transaction id, or None if it cannot be determined.

    `txid_current()` assigns a real xid, which is free here: the caller is about
    to INSERT anyway, so the transaction is a writing one regardless.

    Returning None (rather than raising) on failure degrades the guard to
    "off" for that call. A guard that cannot identify the transaction must not
    be allowed to *block* a legitimate audit row — a missing audit row is a
    worse outcome than a duplicated one, and the structural single-call-site
    rule still holds.
    """
    try:
        cursor = await conn.execute("SELECT txid_current()::text AS txid")
        row = await cursor.fetchone()
    except Exception:  # noqa: BLE001 - see docstring
        logger.debug("audit guard: could not read txid_current()", exc_info=True)
        return None
    if not row:
        return None
    return str(row["txid"] if isinstance(row, dict) else row[0])


def _seen(conn: Any, txid: str) -> set[tuple[str, Optional[str]]]:
    guard = getattr(conn, _GUARD_ATTR, None)
    if guard is None or guard[0] != txid:
        guard = (txid, set())
        try:
            setattr(conn, _GUARD_ATTR, guard)
        except AttributeError:  # pragma: no cover - psycopg allows attributes
            return set()
    return guard[1]


# ---------------------------------------------------------------------------
# step 1 — the writer
# ---------------------------------------------------------------------------

async def write_audit(
    conn: Any,
    *,
    subject_id: str,
    actor_id: str,
    action: str,
    memory_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    allow_repeat: bool = False,
) -> Optional[str]:
    """Insert one `audit_log` row on `conn`. Returns its id, or None if suppressed.

    `conn` must be inside the caller's transaction, opened through
    `store.db.session()` so the `app.subject_id` / `app.actor_id` GUCs are set —
    `audit_log` has FORCE ROW LEVEL SECURITY and an INSERT policy that checks
    both, so an unscoped connection is rejected by the database rather than
    quietly writing an unattributable row.

    A `None` return means the same `(action, memory_id)` was already logged in
    this transaction and the duplicate was dropped. It is never an error path
    for the caller — see the module docstring.

    Pass `allow_repeat=True` when this really is a further distinct action on a
    row already logged in this transaction — the insert-then-reinforce case in
    `persist_candidates()`. The key is still recorded, so an unflagged emission
    from another call site is still caught.
    """
    if action not in ACTIONS:
        raise UnknownAuditAction(
            f"unknown audit action {action!r}; expected one of {sorted(ACTIONS)}"
        )

    key = (action, str(memory_id) if memory_id is not None else None)
    txid = await _transaction_key(conn)
    if txid is not None:
        seen = _seen(conn, txid)
        if key in seen and not allow_repeat:
            AUDIT_DUPLICATES.labels(action=action).inc()
            logger.warning(
                "audit: suppressed duplicate %s row for memory_id=%s in txid=%s "
                "(exactly one audit row per action — see store/audit.py)",
                action,
                memory_id,
                txid,
            )
            return None
        seen.add(key)

    cursor = await conn.execute(
        """
        INSERT INTO audit_log (subject_id, actor_id, memory_id, action, metadata)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        RETURNING id
        """,
        (
            str(subject_id),
            str(actor_id),
            str(memory_id) if memory_id is not None else None,
            action,
            json.dumps(metadata or {}, default=str),
        ),
    )
    row = await cursor.fetchone()
    AUDIT_ROWS.labels(action=action).inc()
    return str(row["id"]) if row else None


async def write_audit_many(
    conn: Any,
    *,
    subject_id: str,
    actor_id: str,
    action: str,
    memory_ids: Sequence[str],
    metadata: dict[str, Any] | None = None,
) -> list[str]:
    """`write_audit()` once per id, in order. Returns the ids actually written.

    Duplicates suppressed by the guard are simply absent from the result, so
    `len(result)` is the honest count of rows added.
    """
    written: list[str] = []
    for memory_id in memory_ids:
        row_id = await write_audit(
            conn,
            subject_id=subject_id,
            actor_id=actor_id,
            action=action,
            memory_id=memory_id,
            metadata=metadata,
        )
        if row_id is not None:
            written.append(row_id)
    return written


# ---------------------------------------------------------------------------
# step 4 — the read hook's transport
# ---------------------------------------------------------------------------

async def record_read_audit(
    subject_id: str,
    actor_id: str,
    memory_ids: Iterable[str],
    *,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Log one `read` row per memory that reached the composed context block.

    THE ONE PLACE THAT OPENS ITS OWN TRANSACTION, and the reason is structural
    rather than convenient. A retrieval is not one transaction: the semantic
    path and the keyword path each open and close their own session, then rank
    and compose run in-process, and only *after* composition is it known which
    memories were actually included. There is no surviving transaction to join
    by the time the answer exists. So the rows go in one transaction of their
    own — every memory of one retrieval logged atomically, which is the
    atomicity guarantee that is actually available here.

    NEVER RAISES. This runs on the live request path from
    `graphs/response_graph.py`, whose entire design premise is that no memory
    subsystem failure may prevent a reply (see its module docstring on why both
    branches of the conditional edge land on `respond`). A failed audit write is
    logged at ERROR and the turn continues. That is a deliberate, stated
    trade-off: the alternative — dropping a user's answer because the audit
    trail was unavailable — is worse, and the failure is loud in the logs and in
    `memsys_audit_rows_total` rather than silent.

    Returns the number of rows written (0 on failure or on an empty list).
    """
    ids = [str(i) for i in memory_ids if i]
    if not ids:
        return 0

    # Imported here, not at module scope: `store.db` is a heavier import and
    # nothing else in this module needs it.
    from store.db import session

    try:
        async with session(subject_id, actor_id) as conn:
            written = await write_audit_many(
                conn,
                subject_id=subject_id,
                actor_id=actor_id,
                action=READ,
                memory_ids=ids,
                metadata={**(metadata or {}), "surfaced": len(ids)},
            )
        return len(written)
    except Exception:  # noqa: BLE001 - see docstring
        logger.error(
            "audit: failed to record read rows for %d memories (subject=%s); "
            "the reply is unaffected",
            len(ids),
            subject_id,
            exc_info=True,
        )
        return 0


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------

async def count_actions(conn: Any, subject_id: str) -> dict[str, int]:
    """`{action: count}` for one subject. Used by the export and by tests."""
    cursor = await conn.execute(
        "SELECT action, count(*) AS n FROM audit_log WHERE subject_id = %s GROUP BY action",
        (str(subject_id),),
    )
    return {row["action"]: int(row["n"]) for row in await cursor.fetchall()}
