"""Fixtures for the M7 governance acceptance suite.

WHAT IS *NOT* HERE, AND MUST NOT BE ADDED
-----------------------------------------
`_isolate_loop_bound_resources` — the autouse fixture that closes loop-bound
connection pools and cancels worker tasks after every test — lives in
`tests/conftest.py` and is inherited here automatically. Do not add a local
copy: two autouse copies would close the pools twice per test. Without it, any
suite touching Postgres across more than ~10 tests eventually dies with
`PoolTimeout` on whichever test happens to be running when the last connection
goes bad, which is never the test that caused it.

The repo-root `conftest.py` similarly supplies the LiteLLM client-cache flush
that keeps live provider calls off a closed event loop.

READS ARE DONE AS THE OWNER, ON PURPOSE
---------------------------------------
Every assertion helper below goes through `store.db.admin_session()` — the
owning role, which bypasses RLS — for the same reason
`tests/integration/conftest.py` documents: a test asserting "the deleted row is
still physically present" or "no audit row was written" has to see *every* row.
Reading as the app role would let RLS hide a row and turn a failing assertion
green, which is exactly the failure mode this milestone is most exposed to.
The endpoints under test never use this connection.

THE EMBEDDING BUDGET
--------------------
The Voyage account this project runs on is capped at **3 requests per minute**,
and a request made past the quota blocks for 12-64 seconds. Two strategies keep
this suite bounded:

  * tests that never exercise semantic retrieval seed rows with
    `synthetic_embedding()` — a deterministic unit vector. Nothing about a
    delete, an export or an audit count depends on the vector meaning anything.
  * the two tests that *do* need real vectors share `real_vectors()`, which
    embeds every text the module needs in ONE batched request and memoises it
    for the process. The provider meters requests, not texts, so N texts in one
    list cost the same as one.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from typing import Any, Iterable, Optional, Sequence

import httpx
import pytest

from store.db import admin_session, embedding_dim, ensure_selector_event_loop_policy, load_env

ensure_selector_event_loop_policy()
load_env()


# ---------------------------------------------------------------------------
# embeddings
# ---------------------------------------------------------------------------

def synthetic_embedding(seed: str) -> list[float]:
    """A deterministic, normalised pseudo-vector of the configured width.

    Real enough for pgvector (finite floats, correct dimension, unit norm so
    cosine distance is well behaved), and free. Used wherever the *content* of
    the vector is irrelevant to what the test is proving.
    """
    dim = embedding_dim()
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    values: list[float] = []
    counter = 0
    while len(values) < dim:
        block = hashlib.sha256(digest + counter.to_bytes(4, "big")).digest()
        values.extend((b - 127.5) / 127.5 for b in block)
        counter += 1
    values = values[:dim]
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


#: Process-wide memo for the one real batched embedding request this module
#: makes. Module-level rather than a fixture because pytest-asyncio gives every
#: test its own event loop, and a session-scoped async fixture would be bound to
#: a loop that is closed by the time the second test runs.
_REAL_VECTORS: dict[str, list[float]] = {}


async def real_vectors(texts: Sequence[str]) -> dict[str, list[float]]:
    """Real Voyage vectors for `texts`, in ONE request, memoised per process.

    Routed through `retrieve.semantic.warm_query_cache` rather than
    `llm.config.embed` directly, because that also populates the retriever's own
    query cache — so a query embedded here is free when `semantic_search` later
    embeds the same string on the live path. That is not a shortcut around the
    provider: the vectors are genuine, and the retrieval under test is the real
    one. It is the difference between 1 request and 8, on a 3-per-minute quota.
    """
    from retrieve.semantic import _MEMORY_CACHE, _cache_key, warm_query_cache
    from llm.config import resolve_embedding_model

    missing = [t for t in texts if t not in _REAL_VECTORS]
    if missing:
        await warm_query_cache(list(dict.fromkeys(missing)))
        model = resolve_embedding_model()
        for text in missing:
            vector = _MEMORY_CACHE.get(_cache_key(model, text))
            if vector is None:  # pragma: no cover - warm_query_cache filled it or raised
                raise AssertionError(f"embedding cache miss for {text!r} after warming")
            _REAL_VECTORS[text] = list(vector)
    return {text: _REAL_VECTORS[text] for text in texts}


# ---------------------------------------------------------------------------
# the store fixture
# ---------------------------------------------------------------------------

class GovernanceStore:
    """Owner-connection view of `memories`, `audit_log` and `feedback`.

    Tracks every subject it hands out and purges all three tables for them at
    teardown, so no test can see another's rows and nothing leaks into the
    integration suites that share this database.
    """

    def __init__(self) -> None:
        self.subjects: list[str] = []

    # -- subjects ----------------------------------------------------------

    def new_subject(self) -> str:
        subject_id = str(uuid.uuid4())
        self.subjects.append(subject_id)
        return subject_id

    # -- seeding -----------------------------------------------------------

    async def seed_memory(
        self,
        subject_id: str,
        content: str,
        *,
        actor_id: Optional[str] = None,
        embedding: Optional[Sequence[float]] = None,
        source: str = "acceptance-seed",
        importance: float = 0.5,
        confidence: float = 0.9,
    ) -> str:
        """Insert one memory through the real store layer. Writes NO audit row.

        `store.memories.insert_memory` is used rather than `persist_candidates`
        precisely because it does not audit: the audit-count tests need a
        subject whose trail contains only the rows the test itself provoked.
        The one test that asserts on the *write* audit row calls
        `persist_candidates` explicitly.
        """
        from store.memories import insert_memory

        actor_id = actor_id or subject_id
        vector = list(embedding) if embedding is not None else synthetic_embedding(content)
        return await insert_memory(
            subject_id, actor_id, content, vector, source, importance, confidence
        )

    async def seed_feedback(
        self,
        subject_id: str,
        memory_id: Optional[str],
        *,
        actor_id: Optional[str] = None,
        signal: str = "up",
        comment: Optional[str] = None,
    ) -> str:
        """Insert one feedback row as the app role, through RLS."""
        from store.db import session

        actor_id = actor_id or subject_id
        async with session(subject_id, actor_id) as conn:
            cursor = await conn.execute(
                "INSERT INTO feedback (subject_id, memory_id, signal, comment) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (subject_id, memory_id, signal, comment),
            )
            return str((await cursor.fetchone())["id"])

    # -- reads (owner connection: RLS cannot hide anything) ----------------

    async def memory_rows(self, subject_id: str) -> list[dict[str, Any]]:
        async with admin_session() as conn:
            cursor = await conn.execute(
                "SELECT id, subject_id, actor_id, content, deleted_at, updated_at,"
                "       (embedding IS NOT NULL) AS has_embedding"
                "  FROM memories WHERE subject_id = %s ORDER BY created_at ASC",
                (subject_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def raw_memory(self, memory_id: str) -> Optional[dict[str, Any]]:
        async with admin_session() as conn:
            cursor = await conn.execute(
                "SELECT id, subject_id, content, deleted_at, embedding::text AS embedding_text"
                "  FROM memories WHERE id = %s",
                (memory_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def audit_rows(
        self,
        subject_id: str,
        *,
        action: Optional[str] = None,
        memory_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        clauses = ["subject_id = %s"]
        params: list[Any] = [subject_id]
        if action is not None:
            clauses.append("action = %s")
            params.append(action)
        if memory_id is not None:
            clauses.append("memory_id = %s")
            params.append(memory_id)
        async with admin_session() as conn:
            cursor = await conn.execute(
                "SELECT id, subject_id, actor_id, memory_id, action, metadata, created_at"
                "  FROM audit_log WHERE " + " AND ".join(clauses) + " ORDER BY created_at ASC",
                tuple(params),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def audit_counts(self, subject_id: str) -> dict[str, int]:
        rows = await self.audit_rows(subject_id)
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["action"]] = counts.get(row["action"], 0) + 1
        return counts

    # -- cleanup -----------------------------------------------------------

    async def purge(self, subjects: Optional[Iterable[str]] = None) -> None:
        targets = list(subjects if subjects is not None else self.subjects)
        if not targets:
            return
        async with admin_session() as conn:
            # audit_log first: it holds a FK to memories, and deleting it up
            # front avoids relying on the ON DELETE SET NULL path during
            # teardown. The owner is the one role the append-only trigger
            # exempts (0006), which is why this is possible at all and why the
            # application role can never do it.
            await conn.execute(
                "DELETE FROM audit_log WHERE subject_id = ANY(%s::uuid[])", (targets,)
            )
            await conn.execute(
                "DELETE FROM feedback WHERE subject_id = ANY(%s::uuid[])", (targets,)
            )
            await conn.execute(
                "DELETE FROM memories WHERE subject_id = ANY(%s::uuid[])", (targets,)
            )


@pytest.fixture
async def gov_store() -> GovernanceStore:
    store = GovernanceStore()
    try:
        yield store
    finally:
        await store.purge()


@pytest.fixture
def subject_a(gov_store: GovernanceStore) -> str:
    return gov_store.new_subject()


@pytest.fixture
def subject_b(gov_store: GovernanceStore) -> str:
    return gov_store.new_subject()


@pytest.fixture
async def api_client():
    """An httpx client wired straight to the ASGI app — no network, no server.

    Deliberately in-process. A dev server started with `python -m api.main`
    serves whatever was on disk when it booted, and stale-server false failures
    have already cost this project three debugging sessions. An ASGI transport
    cannot be stale: it is the code in this working tree, this run.
    """
    from api.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://governance.test", timeout=120.0
    ) as client:
        yield client
