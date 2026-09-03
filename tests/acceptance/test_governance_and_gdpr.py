"""M7 acceptance: audit log, GDPR export, soft-delete.

Fourteen tests, one per plan case. Several of them are easy to write in a form
that passes while proving nothing, and where that is true the test says so and
takes the harder route:

* **Auth boundaries.** M1's row-level security already blocks cross-subject
  access on its own. A verifier on this project demonstrated that deleting the
  application-level `subject_id` predicate from `retrieve/semantic.py` changed
  no test result, because RLS silently covered for it. So
  `test_cannot_delete_another_subjects_memory` and
  `test_export_scoped_to_caller_subject_only` do not merely check that a
  cross-subject request fails — they re-run the production code on a connection
  where RLS is inert (the superuser owner connection) and assert the
  application-level check *still* rejects. Plan step 12 asks for both layers;
  only that construction can tell whether both exist.

* **Append-only.** `test_audit_log_is_append_only` asserts on the SQLSTATE
  PostgreSQL returns (42501), and on the catalog objects in
  `0006_audit_append_only.sql`, rather than on the absence of code that would
  mutate the trail.

* **Concurrency.** `test_concurrent_deletes_write_single_audit_row` runs several
  rounds, because a read-then-write race passes once in a while by luck.

EMBEDDING BUDGET — the Voyage key allows 3 requests/minute, and an over-quota
request blocks for 12-64 seconds. Tests that do not need meaningful vectors seed
`synthetic_embedding()` rows; the ones that do share a single batched request
via `real_vectors()` (see `conftest.py`). Only `test_patch_reembeds_and_audits`
spends a second request, because re-embedding for real is the thing it asserts.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from types import SimpleNamespace

import httpx
import psycopg
import pytest
from fastapi import HTTPException

from store.db import admin_dsn, admin_session, session

from tests.acceptance.conftest import real_vectors, synthetic_embedding

# ---------------------------------------------------------------------------
# the shared text corpus
#
# Every string any test needs a REAL embedding for lives in ALL_TEXTS, and every
# such test warms the whole list. `real_vectors` memoises per process and
# `warm_query_cache` batches, so whichever test runs first pays exactly one
# provider request and the rest are free — regardless of selection or ordering.
# ---------------------------------------------------------------------------

TARGET_CONTENT = "I am severely allergic to shellfish, especially shrimp and crab."
OTHER_CONTENT = "I prefer a window seat on flights longer than four hours."

EXACT_QUERY = "What foods am I allergic to?"
PARAPHRASES = [
    "Do I have any food allergies?",
    "Which ingredients should I avoid when ordering at a restaurant?",
    "Tell me about my seafood allergy.",
]
#: A single bare term that is literally present in TARGET_CONTENT, so
#: `websearch_to_tsquery('english', 'shellfish')` matches its `content_tsv`
#: directly. This is the "exact keyword match" the plan's adversarial case asks
#: for — the query most likely to resurrect a row that the semantic path has
#: forgotten about.
KEYWORD_QUERY = "shellfish"

ALL_TEXTS = [TARGET_CONTENT, OTHER_CONTENT, EXACT_QUERY, *PARAPHRASES, KEYWORD_QUERY]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

async def _hybrid_ids(subject_id: str, text: str) -> set[str]:
    """Memory ids the *live* guarded retrieval path returns for `text`.

    Deliberately `guarded_hybrid_search` — the exact function
    `graphs/response_graph.py` calls — rather than the raw paths, so the
    resurfacing test exercises what production exercises.
    """
    from retrieve.guarded import guarded_hybrid_search
    from retrieve.types import RetrievalQuery

    result = await guarded_hybrid_search(
        RetrievalQuery(text=text, subject_id=subject_id, actor_id=subject_id)
    )
    return {candidate.memory_id for candidate in result.candidates}


async def _semantic_ids(subject_id: str, text: str) -> set[str]:
    from retrieve.semantic import semantic_search
    from retrieve.types import RetrievalQuery

    found = await semantic_search(
        RetrievalQuery(text=text, subject_id=subject_id, actor_id=subject_id)
    )
    return {c.memory_id for c in found}


async def _keyword_ids(subject_id: str, text: str) -> set[str]:
    from retrieve.keyword import keyword_search
    from retrieve.types import RetrievalQuery

    found = await keyword_search(
        RetrievalQuery(text=text, subject_id=subject_id, actor_id=subject_id)
    )
    return {c.memory_id for c in found}


def _candidate(text: str, **overrides):
    """A duck-typed capture candidate for `store.memories.persist_candidates`.

    `persist_candidates` reads its inputs with `getattr`, so a namespace is a
    faithful stand-in and this test needs neither the extraction LLM nor the
    embedding provider to exercise the write path's audit hook.
    """
    fields = {
        "text": text,
        "embedding": synthetic_embedding(text),
        "source": "acceptance",
        "importance": 0.6,
        "confidence": 0.9,
        "dedup_status": "new",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


async def _assert_admin_bypasses_rls() -> None:
    """Guard the two RLS-inert proofs below.

    Those tests are only meaningful if the owner connection really does bypass
    row-level security — `memories` is FORCE ROW LEVEL SECURITY, so ownership
    alone is not enough and it takes a superuser (or BYPASSRLS). This is the
    same property `tests/integration/conftest.py` already relies on. If it ever
    stops holding, the proofs must fail loudly rather than quietly degrade into
    tests of RLS.
    """
    async with admin_session() as conn:
        cursor = await conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        row = await cursor.fetchone()
    assert row is not None, "could not read the admin role from pg_roles"
    assert row["rolsuper"] or row["rolbypassrls"], (
        "the admin connection does not bypass RLS, so an 'RLS is inert' proof "
        "would silently be testing RLS instead of the application-level check"
    )


# ===========================================================================
# 1. the adversarial resurfacing case
# ===========================================================================

@pytest.mark.timeout(300)
async def test_deleted_memory_never_resurfaces_in_retrieval(gov_store, subject_a, api_client):
    """Write, retrieve, delete — then attack it from every angle available.

    The plan calls this one adversarial, so it is not enough to re-run the query
    that found it. After the delete this asserts absence for:

      * the exact query that surfaced it,
      * three paraphrases (different wording, different embedding),
      * an exact keyword match on a distinctive term in its own text,

    and for each of those, through all three code paths: the merged guarded path
    the response graph uses, the semantic path alone, and the keyword path
    alone. Checking only the merged path would let a single-path leak hide
    behind the other path's silence, and the keyword path is the one most likely
    to leak — it does a literal `content_tsv` match and does not care what the
    embedding says.
    """
    vectors = await real_vectors(ALL_TEXTS)

    target_id = await gov_store.seed_memory(
        subject_a, TARGET_CONTENT, embedding=vectors[TARGET_CONTENT]
    )
    await gov_store.seed_memory(subject_a, OTHER_CONTENT, embedding=vectors[OTHER_CONTENT])

    # --- it is genuinely retrievable first, or the rest proves nothing -----
    assert target_id in await _hybrid_ids(subject_a, EXACT_QUERY), (
        "the memory was not retrievable before deletion; the absence assertions "
        "below would then be vacuous"
    )
    assert target_id in await _semantic_ids(subject_a, EXACT_QUERY)
    assert target_id in await _keyword_ids(subject_a, KEYWORD_QUERY), (
        "the keyword path did not match the exact term before deletion"
    )

    # --- delete through the real endpoint ---------------------------------
    response = await api_client.delete(
        f"/memories/{target_id}", params={"subject_id": subject_a}
    )
    assert response.status_code == 200, response.text

    # --- and now it must be gone from everything --------------------------
    probes = [("exact", EXACT_QUERY), ("keyword", KEYWORD_QUERY)]
    probes += [(f"paraphrase{i}", q) for i, q in enumerate(PARAPHRASES, start=1)]

    for label, query in probes:
        assert target_id not in await _hybrid_ids(subject_a, query), (
            f"deleted memory resurfaced in the guarded hybrid path via {label} query {query!r}"
        )
        assert target_id not in await _semantic_ids(subject_a, query), (
            f"deleted memory resurfaced in the semantic path via {label} query {query!r}"
        )
        assert target_id not in await _keyword_ids(subject_a, query), (
            f"deleted memory resurfaced in the keyword path via {label} query {query!r}"
        )

    # The surviving memory is untouched — a delete filter that worked by
    # emptying the result set would pass every assertion above.
    assert await _hybrid_ids(subject_a, OTHER_CONTENT), (
        "retrieval returned nothing at all after the delete; the assertions above "
        "would pass for the wrong reason"
    )


# ===========================================================================
# 2. the curated view
# ===========================================================================

@pytest.mark.timeout(120)
async def test_memories_me_excludes_deleted_row(gov_store, subject_a, api_client):
    """`GET /memories/me` must not contain a soft-deleted memory."""
    kept_id = await gov_store.seed_memory(subject_a, "I run every morning before work.")
    doomed_id = await gov_store.seed_memory(subject_a, "I am learning to play the cello.")

    before = await api_client.get("/memories/me", params={"subject_id": subject_a})
    assert before.status_code == 200, before.text
    assert {m["id"] for m in before.json()} == {kept_id, doomed_id}

    deleted = await api_client.delete(
        f"/memories/{doomed_id}", params={"subject_id": subject_a}
    )
    assert deleted.status_code == 200, deleted.text

    after = await api_client.get("/memories/me", params={"subject_id": subject_a})
    assert after.status_code == 200, after.text
    ids = {m["id"] for m in after.json()}
    assert doomed_id not in ids, "the curated view still lists the deleted memory"
    assert ids == {kept_id}
    # The count header has to agree with the body, or a paginating UI will show
    # a phantom row it can never fetch.
    assert after.headers["X-Total-Count"] == "1"


# ===========================================================================
# 3-4. the GDPR export
# ===========================================================================

@pytest.mark.timeout(120)
async def test_gdpr_export_is_superset_of_curated_view(gov_store, subject_a, api_client):
    """Every curated id appears in the export, and the export holds strictly more."""
    live_ids = {
        await gov_store.seed_memory(subject_a, "My sister's birthday is in March."),
        await gov_store.seed_memory(subject_a, "I take my coffee black."),
    }
    deleted_id = await gov_store.seed_memory(subject_a, "I used to live in Lisbon.")

    assert (
        await api_client.delete(f"/memories/{deleted_id}", params={"subject_id": subject_a})
    ).status_code == 200

    curated = await api_client.get("/memories/me", params={"subject_id": subject_a})
    export = await api_client.get("/memories/export", params={"subject_id": subject_a})
    assert curated.status_code == 200, curated.text
    assert export.status_code == 200, export.text

    curated_ids = {m["id"] for m in curated.json()}
    export_ids = {m["id"] for m in export.json()["memories"]}

    assert curated_ids == live_ids
    assert curated_ids <= export_ids, "the export is missing ids the curated view shows"
    assert export_ids > curated_ids, "the export is not a STRICT superset"
    assert export_ids - curated_ids == {deleted_id}

    # A GDPR dump is more than the memories table.
    payload = export.json()
    assert set(payload) >= {"memories", "audit_log", "feedback", "counts"}
    assert payload["counts"]["memories"] == len(export_ids)
    assert payload["counts"]["memories_deleted"] == 1
    assert payload["counts"]["memories_live"] == len(curated_ids)


@pytest.mark.timeout(120)
async def test_gdpr_export_includes_soft_deleted_with_deletion_marked(
    gov_store, subject_a, api_client
):
    """The deleted row is present, carries `deleted_at`, and is explicitly flagged."""
    deleted_id = await gov_store.seed_memory(subject_a, "I once owned a red bicycle.")
    live_id = await gov_store.seed_memory(subject_a, "I am allergic to penicillin.")

    assert (
        await api_client.delete(f"/memories/{deleted_id}", params={"subject_id": subject_a})
    ).status_code == 200

    export = await api_client.get("/memories/export", params={"subject_id": subject_a})
    assert export.status_code == 200, export.text
    by_id = {m["id"]: m for m in export.json()["memories"]}

    assert deleted_id in by_id, "the export omitted the soft-deleted memory"
    entry = by_id[deleted_id]
    assert entry["deleted_at"] is not None, "deleted_at is not populated in the export"
    assert entry["deleted"] is True, "the deletion is not explicitly flagged (step 10)"
    # The content survives the soft delete — the user is entitled to see what was
    # deleted, not just that something was.
    assert entry["content"] == "I once owned a red bicycle."

    assert by_id[live_id]["deleted"] is False
    assert by_id[live_id]["deleted_at"] is None


# ===========================================================================
# 5-7. exactly one audit row per action
# ===========================================================================

@pytest.mark.timeout(120)
async def test_exactly_one_audit_row_per_write(gov_store, subject_a):
    """One memory write -> exactly one `write` audit row. Not zero, not two.

    Goes through `store.memories.persist_candidates`, which is the single
    sanctioned capture write path and the single place a `write` row is emitted.
    Two is the realistic failure here, not zero: hooking both
    `capture/write.py` and the store layer would double every capture.
    """
    from store.memories import persist_candidates

    results = await persist_candidates(subject_a, subject_a, [_candidate("I drive a blue estate car.")])
    assert len(results) == 1
    assert results[0]["action"] == "insert"
    memory_id = results[0]["memory_id"]

    rows = await gov_store.audit_rows(subject_a, action="write", memory_id=memory_id)
    assert len(rows) == 1, f"expected exactly one write audit row, got {len(rows)}"
    assert str(rows[0]["memory_id"]) == memory_id
    assert str(rows[0]["subject_id"]) == subject_a
    assert str(rows[0]["actor_id"]) == subject_a
    assert rows[0]["metadata"].get("outcome") == "insert"

    # Nothing else was logged for this subject, so the count above is not one
    # row out of several that happen to match.
    assert await gov_store.audit_counts(subject_a) == {"write": 1}

    # A reinforcement is a write too, and must add exactly one more row.
    again = await persist_candidates(
        subject_a, subject_a, [_candidate("I drive a blue estate car.")]
    )
    assert again[0]["action"] == "reinforce"
    assert again[0]["memory_id"] == memory_id
    rows = await gov_store.audit_rows(subject_a, action="write", memory_id=memory_id)
    assert len(rows) == 2, "a reinforcement did not emit exactly one further write row"
    assert [r["metadata"]["outcome"] for r in rows] == ["insert", "reinforce"]


@pytest.mark.timeout(120)
async def test_insert_and_reinforce_in_one_batch_audit_separately(gov_store, subject_a):
    """Two governed actions on one row in ONE transaction earn two audit rows.

    A 15th test, beyond the plan's fourteen, closing a real under-count the
    double-write guard caused. `persist_candidates` can insert a memory for
    candidate A and then, for candidate B in the same batch, dedup onto that very
    row and reinforce it — intra-turn dedup working exactly as M2 designed it.
    Both are governed actions and each is owed a row.

    Keying the guard on `(action, memory_id)` collapsed them into one, and
    `test_exactly_one_audit_row_per_write` could not see it because it makes its
    two `persist_candidates` calls in *separate* transactions, where the guard
    never engages. This test forces both into a single batch, which is the only
    place the bug lives.

    The two candidates share one embedding vector, so B's similarity to A is 1.0
    and the dedup decision under the advisory lock is deterministic rather than
    dependent on how close two real sentences happen to embed.
    """
    from store.memories import persist_candidates

    shared = synthetic_embedding("one-batch-insert-then-reinforce")
    results = await persist_candidates(
        subject_a,
        subject_a,
        [
            _candidate("I am allergic to shellfish.", embedding=shared),
            _candidate("I have a shellfish allergy.", embedding=shared),
        ],
    )

    assert [r["action"] for r in results] == ["insert", "reinforce"], (
        f"the batch did not produce an insert followed by a reinforce: {results}"
    )
    memory_id = results[0]["memory_id"]
    assert results[1]["memory_id"] == memory_id, "the reinforce hit a different row"

    rows = await gov_store.audit_rows(subject_a, action="write", memory_id=memory_id)
    assert len(rows) == 2, (
        f"two governed actions on {memory_id} produced {len(rows)} audit row(s); "
        "the trail under-reports what happened"
    )
    # The MULTISET, not the sequence — and that is a correctness point, not a
    # weakening of the test.
    #
    # Both rows are written in one transaction, `created_at` defaults to now()
    # which is the TRANSACTION timestamp, and `id` is gen_random_uuid(). So the
    # schema records nothing that distinguishes which of these two rows was
    # written first: intra-transaction order is not recoverable, and any
    # assertion about it is really an assertion about the query plan. This one
    # was — it passed while the planner chose a seqscan and failed
    # deterministically once `audit_log` grew enough for an Index Scan Backward
    # over (subject_id, created_at DESC), which returns tied rows reversed.
    #
    # Nothing is lost. This test's claim is in its name — the two actions audit
    # SEPARATELY, two rows rather than one — and the ordering that IS meaningful
    # is already asserted above against `persist_candidates`' return value,
    # which is an ordinary ordered Python list and genuinely records call order.
    outcomes = Counter(r["metadata"].get("outcome") for r in rows)
    assert outcomes == Counter({"insert": 1, "reinforce": 1}), (
        f"expected exactly one insert and one reinforce audit row, got {outcomes}"
    )
    assert await gov_store.audit_counts(subject_a) == {"write": 2}


@pytest.mark.timeout(300)
async def test_exactly_one_audit_row_per_read(gov_store, subject_a, monkeypatch):
    """One retrieval -> exactly one `read` row per memory that reached the prompt.

    Drives `graphs.response_graph.retrieval_node` directly: that is the node the
    hook lives in, and it runs the real guarded retrieval, the real ranker and
    the real composer. Only the LLM call downstream is skipped, and the read hook
    fires before any token exists, so nothing relevant is stubbed.

    WHY FOUR MEMORIES AND A SQUEEZED TOKEN BUDGET
    ---------------------------------------------
    The obvious version of this test seeds ONE memory. It passes, and it proves
    almost nothing: with one memory, "what retrieval returned" and "what the
    composer included" are the same list, so the test cannot tell the correct
    hook from one that audits `result.candidates` — every candidate retrieved,
    including the ones the composer dropped and the model never saw. Auditing a
    memory as *read* when it never reached the prompt is a false entry in a
    governance trail.

    So: four memories, and `CONTEXT_TOKEN_BUDGET` squeezed until the composer has
    room for only some of them. The two lists then genuinely diverge, and the
    assertions below pin the audit rows to the composed set specifically. Under
    the `result.candidates` mutation this test fails — which is the property that
    makes it worth writing.
    """
    from graphs.response_graph import retrieval_node
    from graphs.response_state import new_state

    await real_vectors(ALL_TEXTS)

    # Deliberately verbose, so a small budget cannot fit them all.
    seeded = [
        await gov_store.seed_memory(subject_a, content)
        for content in (
            "I am severely allergic to shellfish, especially shrimp, crab and lobster.",
            "I am mildly allergic to penicillin and carry an antihistamine with me.",
            "I avoid peanuts entirely because they give me a rash and a sore throat.",
            "I cannot eat soft cheese or drink unpasteurised milk without feeling ill.",
        )
    ]

    # Read at call time by `context/config.py`, so setenv is enough — no reload.
    monkeypatch.setenv("CONTEXT_TOKEN_BUDGET", "45")

    # What retrieval FOUND, before the composer gets to choose. `guarded_hybrid_search`
    # writes no audit rows of its own -- only `retrieval_node` does -- so this
    # probe cannot contaminate the counts asserted below.
    retrieved = await _hybrid_ids(subject_a, EXACT_QUERY)
    assert len(retrieved) >= 2, (
        f"retrieval only found {len(retrieved)} of the 4 seeded memories; the "
        "divergence this test depends on would not exist"
    )

    state = new_state(subject_a, subject_a, [{"role": "user", "content": EXACT_QUERY}], emit=None)
    updates = await retrieval_node(state)

    assert updates["degraded"] is False, updates.get("degraded_reason")
    included = list(updates["memory_ids"])

    # The point of the whole setup: the composer kept strictly fewer than
    # retrieval returned. Without this the test degrades into the vacuous
    # one-memory version.
    assert 0 < len(included) < len(retrieved), (
        f"composer included {len(included)} of {len(retrieved)} retrieved memories; "
        "the budget squeeze did not force a drop, so this test cannot distinguish "
        "auditing the composed block from auditing every candidate"
    )
    dropped = set(retrieved) - set(included)
    assert dropped

    rows = await gov_store.audit_rows(subject_a, action="read")
    audited = [str(r["memory_id"]) for r in rows]

    # Exactly one row per included memory — not zero, not duplicated.
    assert sorted(audited) == sorted(included), (
        f"read audit rows {sorted(audited)} do not match the composed block "
        f"{sorted(included)}"
    )
    for memory_id in dropped:
        assert memory_id not in audited, (
            f"memory {memory_id} was dropped by the composer and never reached the "
            "prompt, but was audited as read"
        )
    assert await gov_store.audit_counts(subject_a) == {"read": len(included)}
    assert set(seeded) >= set(retrieved)


@pytest.mark.timeout(120)
async def test_exactly_one_audit_row_per_delete(gov_store, subject_a, api_client):
    """One delete -> exactly one `delete` audit row."""
    memory_id = await gov_store.seed_memory(subject_a, "I keep my passport in the desk drawer.")

    response = await api_client.delete(
        f"/memories/{memory_id}", params={"subject_id": subject_a}
    )
    assert response.status_code == 200, response.text

    rows = await gov_store.audit_rows(subject_a, action="delete")
    assert len(rows) == 1, f"expected exactly one delete audit row, got {len(rows)}"
    assert str(rows[0]["memory_id"]) == memory_id
    assert await gov_store.audit_counts(subject_a) == {"delete": 1}

    # A repeat delete is a no-op and must not append a second row.
    repeat = await api_client.delete(
        f"/memories/{memory_id}", params={"subject_id": subject_a}
    )
    assert repeat.status_code == 404
    assert len(await gov_store.audit_rows(subject_a, action="delete")) == 1


# ===========================================================================
# 8. append-only, enforced by the database
# ===========================================================================

@pytest.mark.timeout(120)
async def test_audit_log_is_append_only(gov_store, subject_a):
    """UPDATE and DELETE on `audit_log` are rejected for the application role.

    The point is *who* rejects them. This asserts on the SQLSTATE PostgreSQL
    returns (42501, insufficient_privilege) and on the catalog state installed
    by `0006_audit_append_only.sql`, so the test cannot be satisfied by a
    convention, a code review, or the mere absence of a mutating query.

    Each statement gets its own session: the first error aborts its transaction,
    and reusing it would make the second failure an
    `InFailedSqlTransaction` — a different error that would pass a naive
    `pytest.raises(Exception)` for entirely the wrong reason.
    """
    from store.audit import WRITE, write_audit
    from store.db import app_db_user

    memory_id = await gov_store.seed_memory(subject_a, "I have a standing dentist appointment.")
    async with session(subject_a, subject_a) as conn:
        audit_id = await write_audit(
            conn, subject_id=subject_a, actor_id=subject_a, action=WRITE, memory_id=memory_id
        )
    assert audit_id is not None

    # --- privilege layer: the grant is genuinely gone ----------------------
    async with admin_session() as conn:
        cursor = await conn.execute(
            "SELECT has_table_privilege(%s, 'audit_log', 'SELECT') AS sel,"
            "       has_table_privilege(%s, 'audit_log', 'INSERT') AS ins,"
            "       has_table_privilege(%s, 'audit_log', 'UPDATE') AS upd,"
            "       has_table_privilege(%s, 'audit_log', 'DELETE') AS del",
            (app_db_user(),) * 4,
        )
        privileges = dict(await cursor.fetchone())
    assert privileges["sel"] is True and privileges["ins"] is True, (
        "the app role must still be able to append to and read the trail"
    )
    assert privileges["upd"] is False, "the app role still holds UPDATE on audit_log"
    assert privileges["del"] is False, "the app role still holds DELETE on audit_log"

    # --- the statements themselves are refused -----------------------------
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as update_error:
        async with session(subject_a, subject_a) as conn:
            await conn.execute(
                "UPDATE audit_log SET action = 'tampered' WHERE id = %s", (audit_id,)
            )
    assert update_error.value.sqlstate == "42501"

    with pytest.raises(psycopg.errors.InsufficientPrivilege) as delete_error:
        async with session(subject_a, subject_a) as conn:
            await conn.execute("DELETE FROM audit_log WHERE id = %s", (audit_id,))
    assert delete_error.value.sqlstate == "42501"

    # --- trigger layer: the net that survives a future re-GRANT ------------
    # The REVOKE above short-circuits before any row is reached, so the trigger
    # is never exercised by the two statements. Assert it exists and is enabled,
    # since it is the layer that keeps this property when someone later writes
    # `GRANT ALL ON ALL TABLES IN SCHEMA public`.
    async with admin_session() as conn:
        cursor = await conn.execute(
            "SELECT t.tgname, t.tgenabled, p.proname"
            "  FROM pg_trigger t"
            "  JOIN pg_class c ON c.oid = t.tgrelid"
            "  JOIN pg_proc  p ON p.oid = t.tgfoid"
            " WHERE c.relname = 'audit_log' AND NOT t.tgisinternal"
        )
        triggers = [dict(row) for row in await cursor.fetchall()]
    assert any(
        row["proname"] == "audit_log_append_only" and row["tgenabled"] == "O"
        for row in triggers
    ), f"the append-only trigger is missing or disabled: {triggers}"

    # --- and the row is untouched ------------------------------------------
    rows = await gov_store.audit_rows(subject_a, action="write")
    assert len(rows) == 1
    assert rows[0]["action"] == "write", "the audit row was mutated after all"


# ===========================================================================
# 9-10. soft-delete semantics
# ===========================================================================

@pytest.mark.timeout(120)
async def test_delete_is_soft_not_hard(gov_store, subject_a, api_client):
    """After a delete the row is still physically present, with `deleted_at` set."""
    content = "I studied civil engineering at university."
    memory_id = await gov_store.seed_memory(subject_a, content)

    response = await api_client.delete(
        f"/memories/{memory_id}", params={"subject_id": subject_a}
    )
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] is True

    # Read as the owner: RLS cannot hide the row, so absence here would mean
    # genuinely gone rather than merely invisible.
    row = await gov_store.raw_memory(memory_id)
    assert row is not None, "the delete was a hard delete — the row is gone"
    assert row["deleted_at"] is not None, "the row survived but deleted_at is NULL"
    assert row["content"] == content, "a soft delete must not destroy the content"


@pytest.mark.timeout(120)
async def test_delete_nonexistent_memory_returns_404(gov_store, subject_a, api_client):
    """A random uuid is a 404 and writes no audit row (the empty-input case)."""
    missing_id = str(uuid.uuid4())

    response = await api_client.delete(
        f"/memories/{missing_id}", params={"subject_id": subject_a}
    )
    assert response.status_code == 404, response.text

    assert await gov_store.audit_rows(subject_a) == [], (
        "a failed delete wrote an audit row; the trail must record what happened, "
        "not what was attempted and refused"
    )
    # Nothing was logged anywhere against that id either, for any subject.
    async with admin_session() as conn:
        cursor = await conn.execute(
            "SELECT count(*) AS n FROM audit_log WHERE memory_id = %s", (missing_id,)
        )
        assert int((await cursor.fetchone())["n"]) == 0


# ===========================================================================
# 11-12. auth boundaries — both layers, proven separately
# ===========================================================================

@pytest.mark.timeout(120)
async def test_cannot_delete_another_subjects_memory(gov_store, subject_a, subject_b, api_client):
    """Subject B cannot delete subject A's memory — and the API check is real.

    The first half is the plain behavioural assertion. It would pass with the
    application-level ownership check deleted from `api/memories.py`, because
    RLS already hides A's row from B: the UPDATE would match zero rows and the
    endpoint would answer 404 regardless.

    The second half is therefore the one that carries the weight. It calls
    `api.memories.ensure_owned` — the production function, not a copy — on the
    superuser owner connection, where RLS is inert and A's row is plainly
    visible. If the subject comparison inside it were removed, that call would
    return the row instead of raising, and this test would fail. That is exactly
    the independence the milestone asks for.
    """
    from api.memories import ensure_owned

    memory_id = await gov_store.seed_memory(subject_a, "My spare key is with the neighbour.")

    # --- layer under test 1: the endpoint ---------------------------------
    response = await api_client.delete(
        f"/memories/{memory_id}", params={"subject_id": subject_b}
    )
    assert response.status_code in (403, 404), response.text

    row = await gov_store.raw_memory(memory_id)
    assert row is not None and row["deleted_at"] is None, "A's row was modified by B"
    assert await gov_store.audit_rows(subject_b) == []
    assert await gov_store.audit_rows(subject_a) == []

    # --- layer under test 2: the check itself, with RLS out of the picture -
    await _assert_admin_bypasses_rls()
    async with admin_session() as conn:
        cursor = await conn.execute("SELECT id FROM memories WHERE id = %s", (memory_id,))
        assert await cursor.fetchone() is not None, (
            "the row is not visible on the admin connection, so RLS is still "
            "filtering and the proof below would be meaningless"
        )

        with pytest.raises(HTTPException) as forbidden:
            await ensure_owned(conn, memory_id, subject_b)
        assert forbidden.value.status_code == 403, (
            "ensure_owned did not reject a cross-subject row on a connection "
            "where RLS was not filtering — the application-level check is absent"
        )

        # The same call for the rightful owner succeeds, so the rejection above
        # is the subject comparison and not a blanket refusal.
        owned = await ensure_owned(conn, memory_id, subject_a)
        assert str(owned["id"]) == memory_id


@pytest.mark.timeout(120)
async def test_export_scoped_to_caller_subject_only(
    gov_store, subject_a, subject_b, api_client, monkeypatch
):
    """A's export contains none of B's rows — by application predicate, not only RLS.

    As above, the behavioural half alone proves little: RLS would scope the
    export on its own. So the second half re-runs the *same endpoint function*
    with `store.db.session` swapped for one opened on the superuser DSN. RLS is
    then inert — B's memories, audit rows and feedback are all visible to that
    connection — and the export must still return only A's, which can only be
    the `WHERE subject_id = %s` predicates in `api/governance.py` doing the work.

    Note what is NOT worked around here: `feedback` has no `actor_id` column
    (M1 plan step 11), so 0005 scopes its RLS on `subject_id` equality plus
    `app.actor_id IS NOT NULL` — presence, not equality. For a subject-scoped
    export that is harmless, and the SQL predicate below is what makes it so.
    """
    import api.governance as governance

    a_ids = {
        await gov_store.seed_memory(subject_a, "I volunteer at the animal shelter."),
        await gov_store.seed_memory(subject_a, "I am saving for a trip to Japan."),
    }
    b_ids = {
        await gov_store.seed_memory(subject_b, "I am training for a half marathon."),
        await gov_store.seed_memory(subject_b, "I have a peanut allergy."),
    }
    a_feedback = await gov_store.seed_feedback(subject_a, sorted(a_ids)[0], signal="up")
    b_feedback = await gov_store.seed_feedback(subject_b, sorted(b_ids)[0], signal="down")

    # Give B an audit trail too, so "no B audit rows" is a real constraint.
    b_deleted = sorted(b_ids)[0]
    assert (
        await api_client.delete(f"/memories/{b_deleted}", params={"subject_id": subject_b})
    ).status_code == 200

    def _assert_scoped(payload: dict, *, where: str) -> None:
        memory_ids = {m["id"] for m in payload["memories"]}
        assert memory_ids == a_ids, f"{where}: export memories were {memory_ids}, expected {a_ids}"
        assert not (memory_ids & b_ids), f"{where}: the export leaked B's memories"

        audit_subjects = {r["subject_id"] for r in payload["audit_log"]}
        assert subject_b not in audit_subjects, f"{where}: the export leaked B's audit rows"
        assert audit_subjects <= {subject_a}

        feedback_ids = {r["id"] for r in payload["feedback"]}
        assert b_feedback not in feedback_ids, f"{where}: the export leaked B's feedback"
        assert feedback_ids == {a_feedback}

    # --- behavioural: the shipped stack (predicate AND RLS) ---------------
    response = await api_client.get("/memories/export", params={"subject_id": subject_a})
    assert response.status_code == 200, response.text
    _assert_scoped(response.json(), where="with RLS active")

    # --- independent: the same code with RLS unable to help ---------------
    await _assert_admin_bypasses_rls()

    real_session = governance.session

    def _superuser_session(subject_id, actor_id, **kwargs):
        # Same GUCs, same transaction semantics — only the role differs, and
        # that role bypasses RLS entirely.
        kwargs.pop("dsn", None)
        return real_session(subject_id, actor_id, dsn=admin_dsn(), **kwargs)

    monkeypatch.setattr(governance, "session", _superuser_session)

    unguarded = await api_client.get("/memories/export", params={"subject_id": subject_a})
    assert unguarded.status_code == 200, unguarded.text
    _assert_scoped(unguarded.json(), where="with RLS inert")

    # Sanity: that connection really could have seen B's rows.
    async with real_session(subject_a, subject_a, dsn=admin_dsn()) as conn:
        cursor = await conn.execute(
            "SELECT count(*) AS n FROM memories WHERE subject_id = %s", (subject_b,)
        )
        assert int((await cursor.fetchone())["n"]) == len(b_ids), (
            "B's rows were not visible to the unguarded connection, so the check "
            "above did not actually run with RLS inert"
        )


# ===========================================================================
# 13. edit + re-embed
# ===========================================================================

@pytest.mark.timeout(300)
async def test_patch_reembeds_and_audits(gov_store, subject_a, api_client):
    """PATCH replaces content, the stored vector changes, one `update` row lands.

    This is the one test that deliberately spends a real embedding request —
    asserting "the embedding changed" against a stubbed embedder would prove
    only that the stub returned something different. The timeout is generous
    because a request past the 3/minute quota blocks for 12-64 seconds; that is
    provider backoff, not a hang.
    """
    memory_id = await gov_store.seed_memory(subject_a, "I am learning Portuguese.")
    before = await gov_store.raw_memory(memory_id)
    assert before is not None and before["embedding_text"] is not None

    new_content = "I am learning Japanese and practising kanji every evening."
    response = await api_client.patch(
        f"/memories/{memory_id}",
        params={"subject_id": subject_a},
        json={"content": new_content},
    )
    assert response.status_code == 200, response.text
    assert response.json()["content"] == new_content

    after = await gov_store.raw_memory(memory_id)
    assert after is not None
    assert after["content"] == new_content
    assert after["embedding_text"] != before["embedding_text"], (
        "the content changed but the stored embedding did not — the memory would "
        "stay semantically findable under its old meaning"
    )
    assert after["deleted_at"] is None

    rows = await gov_store.audit_rows(subject_a, action="update")
    assert len(rows) == 1, f"expected exactly one update audit row, got {len(rows)}"
    assert str(rows[0]["memory_id"]) == memory_id
    assert rows[0]["metadata"].get("reembedded") is True
    assert await gov_store.audit_counts(subject_a) == {"update": 1}


# ===========================================================================
# 14. concurrency
# ===========================================================================

@pytest.mark.timeout(300)
async def test_concurrent_deletes_write_single_audit_row(gov_store, subject_a):
    """Two simultaneous deletes: one succeeds, and exactly one audit row exists.

    Run over several rounds with a fresh memory each time. A single round proves
    little — a read-then-write race loses often enough to pass by luck, which is
    precisely the bug this is meant to catch.

    Two independent ASGI clients are used so the two requests cannot be
    serialised by a shared client's connection handling; both share the process
    connection pool, so they contend on the database exactly as two real
    requests would.
    """
    from api.main import app

    rounds = 5
    for attempt in range(1, rounds + 1):
        memory_id = await gov_store.seed_memory(
            subject_a, f"Concurrent delete probe number {attempt}."
        )

        async def _delete() -> int:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://governance.test", timeout=60.0
            ) as client:
                response = await client.delete(
                    f"/memories/{memory_id}", params={"subject_id": subject_a}
                )
                return response.status_code

        first, second = await asyncio.gather(_delete(), _delete())
        statuses = sorted([first, second])

        assert statuses == [200, 404], (
            f"round {attempt}: expected exactly one success and one 404, got {statuses}"
        )

        rows = await gov_store.audit_rows(subject_a, action="delete", memory_id=memory_id)
        assert len(rows) == 1, (
            f"round {attempt}: expected exactly one delete audit row, got {len(rows)}"
        )

        row = await gov_store.raw_memory(memory_id)
        assert row is not None and row["deleted_at"] is not None

    # One row per round overall — no round leaked an extra into another's count.
    all_deletes = await gov_store.audit_rows(subject_a, action="delete")
    assert len(all_deletes) == rounds
