"""M1 integration tests — 10 of the milestone's 11 named cases.

These run against the live compose stack and the live providers. They are
integration tests by design: the point of M1 is that the infrastructure is real,
so mocking any of it here would test nothing.

Run:  pytest tests/integration/test_m1_infra.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient

from llm.config import embed, complete, resolve_embedding_dim
from store.db import admin_dsn, app_dsn

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def admin_conn():
    """Owner connection. Catalog inspection only — never used to prove RLS."""
    with psycopg.connect(admin_dsn()) as conn:
        yield conn


def _indexdefs(conn, table: str) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s",
            (table,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# 1. health endpoint
# ---------------------------------------------------------------------------

async def test_health_endpoint_returns_200():
    """GET /health -> 200 with postgres and redis both true."""
    from api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["postgres"] is True, f"postgres unreachable: {body}"
    assert body["redis"] is True, f"redis unreachable: {body}"
    assert body["status"] == "ok", body


# ---------------------------------------------------------------------------
# 2 & 3. indexes
# ---------------------------------------------------------------------------

def test_memories_table_has_hnsw_index_on_embedding(admin_conn):
    defs = _indexdefs(admin_conn, "memories")
    assert defs, "memories table has no indexes at all — did migrations run?"
    hnsw = [(n, d) for n, d in defs if "USING hnsw" in d and "embedding" in d]
    assert hnsw, f"no HNSW index on memories.embedding; found: {defs}"
    assert "vector_cosine_ops" in hnsw[0][1], hnsw[0][1]


def test_memories_table_has_gin_index_on_content_tsv(admin_conn):
    defs = _indexdefs(admin_conn, "memories")
    gin = [(n, d) for n, d in defs if "USING gin" in d and "content_tsv" in d]
    assert gin, f"no GIN index on memories.content_tsv; found: {defs}"


# ---------------------------------------------------------------------------
# 4 & 5. RLS enabled, and policies scope both seam columns
# ---------------------------------------------------------------------------

def test_rls_enabled_on_memories(admin_conn):
    with admin_conn.cursor() as cur:
        cur.execute("SELECT relrowsecurity FROM pg_class WHERE relname = 'memories'")
        row = cur.fetchone()
    assert row is not None, "memories table does not exist"
    assert row[0] is True, "relrowsecurity is not 't' on memories"


def test_rls_policies_scope_both_subject_and_actor(admin_conn):
    """Every policy's qualifier must reference BOTH subject_id and actor_id.

    NOTE: PostgreSQL forbids USING on an INSERT policy, so pg_policies.qual is
    NULL for the INSERT policy and its predicate lives in with_check. The
    qualifier under test is therefore coalesce(qual, with_check).
    """
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT policyname, cmd, qual, with_check FROM pg_policies "
            "WHERE tablename = 'memories'"
        )
        policies = cur.fetchall()

    assert policies, "memories has RLS enabled but zero policies (fails closed, but wrong)"
    commands = {cmd for _, cmd, _, _ in policies}
    assert commands >= {"SELECT", "INSERT", "UPDATE", "DELETE"}, commands

    for name, cmd, qual, with_check in policies:
        qualifier = qual if qual is not None else with_check
        assert qualifier, f"policy {name} ({cmd}) has no predicate at all"
        assert "subject_id" in qualifier, f"policy {name} ({cmd}) omits subject_id: {qualifier}"
        assert "actor_id" in qualifier, f"policy {name} ({cmd}) omits actor_id: {qualifier}"


# ---------------------------------------------------------------------------
# 6 & 7. the LiteLLM seam, live
# ---------------------------------------------------------------------------

async def test_litellm_completion_returns_nonempty():
    """A real completion through llm/config.py using the configured LLM_MODEL."""
    text = await complete(
        [{"role": "user", "content": "Reply with exactly one word: pong"}]
    )
    assert isinstance(text, str)
    assert text.strip(), (
        "empty completion — if this is a gpt-oss model, max_tokens is too low "
        "(reasoning tokens are spent before content); the wrapper floors it at 512"
    )


async def test_litellm_embedding_returns_nonempty_vector():
    """A real embedding through llm/config.py; width must equal EMBEDDING_DIM."""
    vectors = await embed(["hello"])
    assert len(vectors) == 1, vectors
    assert len(vectors[0]) == resolve_embedding_dim(), (
        f"got {len(vectors[0])} dims, EMBEDDING_DIM is {resolve_embedding_dim()}"
    )
    assert any(abs(v) > 0 for v in vectors[0]), "embedding is an all-zero vector"


# ---------------------------------------------------------------------------
# 8. audit_log + feedback exist with RLS
# ---------------------------------------------------------------------------

def test_audit_log_and_feedback_tables_exist(admin_conn):
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT relname, relrowsecurity FROM pg_class "
            "WHERE relname IN ('audit_log', 'feedback') AND relkind = 'r'"
        )
        found = dict(cur.fetchall())

    assert set(found) == {"audit_log", "feedback"}, f"missing tables: {found}"
    for table, rls in found.items():
        assert rls is True, f"RLS is not enabled on {table}"


# ---------------------------------------------------------------------------
# 9. the auth boundary — this is the test RLS exists for
# ---------------------------------------------------------------------------

def test_rls_blocks_cross_subject_read():
    """Insert as subject A, read as subject B, get zero rows.

    Connects as the NON-superuser application role. Connecting as the owner or a
    superuser would bypass RLS and make this test pass vacuously, which is the
    single most common way this milestone silently fails — so the test asserts
    up front that the role it is using is neither superuser nor bypassrls.
    """
    subject_a = str(uuid.uuid4())
    subject_b = str(uuid.uuid4())
    content = f"rls-probe-{uuid.uuid4()}"

    with psycopg.connect(app_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"
            )
            role, is_super, bypasses = cur.fetchone()
        assert not is_super, f"app role {role!r} is a superuser — RLS would be bypassed"
        assert not bypasses, f"app role {role!r} has BYPASSRLS — RLS would be bypassed"

        with conn.cursor() as cur:
            cur.execute(
                "SELECT tableowner FROM pg_tables WHERE tablename = 'memories'"
            )
            owner = cur.fetchone()[0]
        assert owner != role, f"app role {role!r} owns memories — RLS would be bypassed"

        # --- write as subject A ------------------------------------------
        with conn.transaction():
            conn.execute(
                "SELECT set_config('app.subject_id', %s, true),"
                "       set_config('app.actor_id',   %s, true)",
                (subject_a, subject_a),
            )
            conn.execute(
                "INSERT INTO memories (subject_id, actor_id, content, source) "
                "VALUES (%s, %s, %s, 'rls-test')",
                (subject_a, subject_a, content),
            )
            cur = conn.execute(
                "SELECT count(*) FROM memories WHERE content = %s", (content,)
            )
            assert cur.fetchone()[0] == 1, "subject A cannot see its own freshly written row"

        # --- read as subject B -------------------------------------------
        with conn.transaction():
            conn.execute(
                "SELECT set_config('app.subject_id', %s, true),"
                "       set_config('app.actor_id',   %s, true)",
                (subject_b, subject_b),
            )
            cur = conn.execute(
                "SELECT count(*) FROM memories WHERE content = %s", (content,)
            )
            assert cur.fetchone()[0] == 0, "subject B can read subject A's memory — RLS is not enforcing"

        # --- and with no GUCs set at all, RLS must fail closed -----------
        with conn.transaction():
            cur = conn.execute("SELECT count(*) FROM memories")
            assert cur.fetchone()[0] == 0, "unscoped session sees rows — RLS fails open"

    # cleanup as owner
    with psycopg.connect(admin_dsn()) as conn:
        conn.execute("DELETE FROM memories WHERE content = %s", (content,))
        conn.commit()


# ---------------------------------------------------------------------------
# 10. migrations are idempotent
# ---------------------------------------------------------------------------

def test_migrations_are_idempotent(admin_conn):
    """Run `python -m store.migrate` twice; second run must exit 0 and add nothing."""

    def object_census() -> dict:
        with admin_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_indexes WHERE tablename IN "
                "('memories','audit_log','feedback')"
            )
            indexes = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM pg_policies WHERE tablename IN "
                "('memories','audit_log','feedback')"
            )
            policies = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'memories'"
            )
            columns = cur.fetchone()[0]
        return {"indexes": indexes, "policies": policies, "columns": columns}

    def run() -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "store.migrate"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )

    first = run()
    assert first.returncode == 0, f"first migrate run failed:\n{first.stdout}\n{first.stderr}"
    admin_conn.rollback()  # refresh this connection's snapshot
    before = object_census()

    second = run()
    assert second.returncode == 0, f"second migrate run failed:\n{second.stdout}\n{second.stderr}"
    assert "ERROR" not in second.stderr.upper(), second.stderr
    admin_conn.rollback()
    after = object_census()

    assert before == after, f"second migration run changed the schema: {before} -> {after}"
