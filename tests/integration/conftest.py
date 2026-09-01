"""Integration fixtures for the capture pipeline (plan step 15).

Three things live here:

* a **clean per-test `subject_id`** -- a fresh uuid4 per test, so no test can
  see another's rows and the RLS boundary is exercised for real;
* a **truncating DB fixture** -- `store.purge()`, run automatically at teardown;
* a **bounded poller** -- `store.poll_for_rows()`, which waits for capture to
  land instead of sleeping a guessed interval.

WHY DELETE BY SUBJECT RATHER THAN `TRUNCATE memories`
Capture is asynchronous, several suites share one database, and a global
TRUNCATE would delete rows another test (or another developer's session) is
mid-way through writing. Every test here allocates its own uuid subject, so
deleting `WHERE subject_id = ANY(...)` is both complete for the test and safe
for everything else. `truncate_memories()` is provided for the rare caller that
genuinely wants the whole table, but nothing uses it by default.

WHY THE READ-BACK USES THE ADMIN CONNECTION
Assertions read through `store.db.admin_session()` -- the owning superuser,
which bypasses RLS. That is deliberate: a test asserting "the raw SSN is not in
the `content` column" has to see *every* row, including any that RLS would have
hidden. Reading as the app role could make a leak invisible and turn a failing
assertion green. The production path never uses this connection.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Iterable

import httpx
import pytest

from capture.metrics import configure_logging
from store.db import admin_session, ensure_selector_event_loop_policy, load_env

ensure_selector_event_loop_policy()
load_env()

# The M2 Definition of Done asks to confirm capture behaviour "from that test's
# output/logs". Without this the capture logger sits at root's default WARNING
# and emits nothing, so `-o log_cli=true --log-cli-level=INFO` would show an
# empty log however correct the run was.
configure_logging()

#: Default bound for "wait for capture to land". Capture makes two completion
#: round-trips (extract + evaluate) plus an embedding call, and the embedding
#: call may sit out a provider rate-limit window (see `capture/embed.py`), so
#: the bound is generous. It is still a hard bound -- the poller returns and the
#: test asserts; it never waits forever.
DEFAULT_POLL_TIMEOUT = 600.0
POLL_INTERVAL = 0.25


class MemoryStore:
    """Test-side view of the `memories` table, plus subject bookkeeping."""

    def __init__(self) -> None:
        self.subjects: list[str] = []

    # -- subjects ----------------------------------------------------------

    def new_subject(self) -> str:
        """Allocate a fresh, tracked subject id. Purged at teardown."""
        subject_id = str(uuid.uuid4())
        self.subjects.append(subject_id)
        return subject_id

    def track(self, subject_id: str) -> str:
        if subject_id not in self.subjects:
            self.subjects.append(subject_id)
        return subject_id

    # -- reads -------------------------------------------------------------

    async def rows(self, subject_id: str, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        """Every stored row for `subject_id`, oldest first. Bypasses RLS on purpose."""
        clause = "" if include_deleted else " AND deleted_at IS NULL"
        async with admin_session() as conn:
            cursor = await conn.execute(
                "SELECT id, subject_id, actor_id, content, source, importance, confidence,"
                "       weight, reinforcement_count, created_at, updated_at,"
                "       last_accessed_at, deleted_at,"
                "       (embedding IS NOT NULL) AS has_embedding"
                "  FROM memories"
                f" WHERE subject_id = %s{clause}"
                " ORDER BY created_at ASC",
                (subject_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def count(self, subject_id: str) -> int:
        return len(await self.rows(subject_id))

    async def contents(self, subject_id: str) -> list[str]:
        return [row["content"] for row in await self.rows(subject_id)]

    async def poll_for_rows(
        self,
        subject_id: str,
        *,
        minimum: int = 1,
        timeout: float = DEFAULT_POLL_TIMEOUT,
        interval: float = POLL_INTERVAL,
    ) -> list[dict[str, Any]]:
        """Poll until at least `minimum` rows exist, or the bound expires.

        Returns whatever it has when the deadline passes -- the caller asserts.
        A fixture that raised on timeout would mask "capture wrote nothing" as a
        fixture error rather than a test failure.
        """
        deadline = time.monotonic() + timeout
        rows: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            rows = await self.rows(subject_id)
            if len(rows) >= minimum:
                return rows
            await asyncio.sleep(interval)
        return rows

    async def poll_until(
        self,
        subject_id: str,
        predicate,
        *,
        timeout: float = DEFAULT_POLL_TIMEOUT,
        interval: float = POLL_INTERVAL,
    ) -> list[dict[str, Any]]:
        """Poll until `predicate(rows)` is true, or the bound expires."""
        deadline = time.monotonic() + timeout
        rows: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            rows = await self.rows(subject_id)
            if predicate(rows):
                return rows
            await asyncio.sleep(interval)
        return rows

    # -- writes / cleanup --------------------------------------------------

    async def purge(self, subjects: Iterable[str] | None = None) -> int:
        """Delete this test's rows from `memories`, `audit_log` and `feedback`.

        The audit/feedback cleanup is not optional. Since M7, every capture
        write and every retrieval emits `audit_log` rows, so a purge that only
        touched `memories` left orphans behind — 34 rows across 21 subjects were
        found accumulating before this was fixed. That matters beyond untidiness:
        M7's `test_exactly_one_audit_row_per_*` tests count rows, and a slowly
        filling table is exactly the sort of shared state that makes an
        unrelated test fail later.

        `audit_log` is append-only for the *app* role; this runs on the admin
        connection, which is the deliberate carve-out for test cleanup.
        """
        targets = list(subjects if subjects is not None else self.subjects)
        if not targets:
            return 0
        async with admin_session() as conn:
            cursor = await conn.execute(
                "DELETE FROM memories WHERE subject_id = ANY(%s::uuid[])", (targets,)
            )
            deleted = cursor.rowcount or 0
            for table in ("audit_log", "feedback"):
                await conn.execute(
                    f"DELETE FROM {table} WHERE subject_id = ANY(%s::uuid[])", (targets,)
                )
            return deleted

    async def truncate_memories(self) -> None:
        """Whole-table wipe. Nothing uses this by default -- see module docstring."""
        async with admin_session() as conn:
            await conn.execute("TRUNCATE memories")


# NOTE: the LiteLLM client-cache flush fixture that used to live here has been
# promoted to the repo-root `conftest.py`, per M2's own recommendation, so that
# `tests/unit/` making live provider calls is covered too. It is autouse there
# and applies to every test in the tree; do not re-add a local copy.


# NOTE: `_isolate_loop_bound_resources` — which closes loop-bound connection
# pools and cancels worker tasks after every test — has been promoted to
# `tests/conftest.py`, so the suites added by M5 (`tests/reliability/`), M7
# (`tests/acceptance/`) and M8 (`tests/distributed/`) inherit it rather than each
# rediscovering the `PoolTimeout` failure it prevents. Do not re-add a local
# copy: two autouse copies would close the pools twice per test.


@pytest.fixture
async def store() -> MemoryStore:
    """Per-test table access. Purges every subject it handed out at teardown."""
    fixture = MemoryStore()
    try:
        yield fixture
    finally:
        await fixture.purge()


@pytest.fixture
def subject_id(store: MemoryStore) -> str:
    """A clean, isolated subject id for this test."""
    return store.new_subject()


@pytest.fixture
def actor_id(subject_id: str) -> str:
    """Single-user mode: the actor is the subject (M1's schema seam)."""
    return subject_id


@pytest.fixture
async def capture_worker():
    """A capture worker bound to this test's event loop, torn down afterwards.

    pytest-asyncio builds a fresh loop per test; the module-level worker
    singleton would otherwise keep tasks and a queue attached to a closed one.
    """
    from capture.worker import get_worker, reset_worker

    await reset_worker()
    worker = get_worker()
    try:
        yield worker
    finally:
        await reset_worker()


@pytest.fixture
async def chat_client(capture_worker):
    """An httpx client wired straight to the ASGI app -- no network, no server.

    Depends on `capture_worker` so the background pool is always bound to the
    same loop the request runs on.
    """
    from api.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://capture.test") as client:
        yield client
