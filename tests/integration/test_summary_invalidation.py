"""M9 integration tests — changing a source invalidates the summary built on it.

THE BUG THESE PIN, reproduced from a real user's data
-----------------------------------------------------
M7 promises erasure. M8 added a reflection agent that folds raw memories into
summaries. Nobody joined the two:

  1. the user stated two preferences, Java and Python
  2. the nightly reflection agent wrote one summary quoting both
  3. the user DELETED both preferences — `deleted_at` set correctly on each
  4. the summary was untouched, still live, still retrievable, so the assistant
     kept answering in Java and Python and citing the preferences

They deleted the two sentences; they never deleted the paragraph that quotes
them. The delete APPEARED to work — the API returned success and the audit trail
agreed — and the fact reached the model anyway. That is the worst possible place
for a gap, because nothing about it looks broken from outside.

The dependency edge was already in the schema (`consolidated_into`, added by
0007, with an index). Nothing had ever read it back.

Run:  pytest tests/integration/test_summary_invalidation.py -v
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobs.reflection import REFLECTION_SOURCE, rebuild_stale_summaries  # noqa: E402
from retrieve.keyword import keyword_search  # noqa: E402
from retrieve.types import RetrievalQuery  # noqa: E402
from store.db import admin_session, session  # noqa: E402
from store.memories import insert_memory, mark_summary_stale  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]

EMBEDDING_DIM = 1024


def _vector(slot: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[slot % EMBEDDING_DIM] = 1.0
    return vector


@pytest.fixture
async def subject():
    subject_id = str(uuid.uuid4())
    try:
        yield subject_id
    finally:
        async with admin_session() as conn:
            await conn.execute(
                "DELETE FROM audit_log WHERE subject_id = %s::uuid", (subject_id,)
            )
            await conn.execute(
                "UPDATE memories SET consolidated_into = NULL, superseded_by = NULL, "
                "superseded_at = NULL "
                "WHERE subject_id = %s::uuid",
                (subject_id,),
            )
            await conn.execute(
                "DELETE FROM memories WHERE subject_id = %s::uuid", (subject_id,)
            )


async def _seed_summary_with_sources(
    subject_id: str, source_texts: list[str], summary_text: str
) -> tuple[list[str], str]:
    """Two source memories consolidated into one summary, as reflection leaves them."""
    async with session(subject_id, subject_id) as conn:
        source_ids = [
            await insert_memory(
                subject_id, subject_id, text, _vector(index + 1),
                "user_preference", 0.7, 0.9, conn=conn,
            )
            for index, text in enumerate(source_texts)
        ]
        summary_id = await insert_memory(
            subject_id, subject_id, summary_text, _vector(90),
            REFLECTION_SOURCE, 0.6, 0.7, conn=conn,
        )
        await conn.execute(
            "UPDATE memories SET consolidated_at = now(), consolidated_into = %s::uuid"
            " WHERE id = ANY(%s::uuid[])",
            (summary_id, source_ids),
        )
    return source_ids, summary_id


async def _row(memory_id: str) -> dict:
    async with admin_session() as conn:
        cursor = await conn.execute(
            "SELECT id, content, stale_at, deleted_at, consolidated_at, consolidated_into"
            "  FROM memories WHERE id = %s::uuid",
            (memory_id,),
        )
        return dict(await cursor.fetchone())


async def _delete_source(subject_id: str, source_id: str, reason: str) -> list[dict]:
    """Soft-delete a source and cascade, the way `DELETE /memories/{id}` does."""
    async with session(subject_id, subject_id) as conn:
        await conn.execute(
            "UPDATE memories SET deleted_at = now() WHERE id = %s::uuid", (source_id,)
        )
        return await mark_summary_stale(
            conn,
            subject_id=subject_id,
            actor_id=subject_id,
            source_id=source_id,
            reason=reason,
        )


async def test_deleting_a_source_marks_its_summary_stale(subject):
    """The core cascade. Without it the summary keeps quoting the erased fact."""
    sources, summary_id = await _seed_summary_with_sources(
        subject,
        ["The user prefers Java.", "The user prefers to receive code in Python."],
        "The user prefers Java but also likes receiving code in Python.",
    )

    assert (await _row(summary_id))["stale_at"] is None, "not stale before the delete"

    stale = await _delete_source(subject, sources[0], "source_deleted")

    assert [str(row["id"]) for row in stale] == [summary_id]
    assert (await _row(summary_id))["stale_at"] is not None


async def test_a_stale_summary_never_reaches_retrieval(subject):
    """THE PROPERTY THAT ACTUALLY MATTERS.

    Marking a column is worthless if the row still reaches the model. This
    drives the real keyword path — chosen because it needs no embedding, so it
    exercises the production query with no provider call.
    """
    sources, _ = await _seed_summary_with_sources(
        subject,
        ["The user prefers Java."],
        "PremSiva works at TCS and prefers Java.",
    )

    before = [c.content for c in await keyword_search(
        RetrievalQuery(text="Java", subject_id=subject, actor_id=subject)
    )]
    assert any("PremSiva" in c for c in before), (
        f"the summary was not retrievable to begin with, so this test would "
        f"pass for the wrong reason: {before}"
    )

    await _delete_source(subject, sources[0], "source_deleted")

    after = [c.content for c in await keyword_search(
        RetrievalQuery(text="Java", subject_id=subject, actor_id=subject)
    )]
    assert not any("PremSiva" in c for c in after), (
        f"the summary still reached retrieval after its source was deleted — "
        f"this is the reported bug: {after}"
    )
    assert after == [], f"nothing about Java should remain retrievable: {after}"


async def test_editing_a_source_marks_the_summary_stale(subject):
    """An edit invalidates too, and is arguably worse than a delete.

    After an edit the summary asserts something the user has explicitly
    corrected, and unlike a deletion there is no missing row to hint that
    anything changed.
    """
    sources, summary_id = await _seed_summary_with_sources(
        subject, ["The user works at TCS."], "The user works at TCS."
    )

    async with session(subject, subject) as conn:
        await conn.execute(
            "UPDATE memories SET content = %s WHERE id = %s::uuid",
            ("The user works at Infosys.", sources[0]),
        )
        stale = await mark_summary_stale(
            conn, subject_id=subject, actor_id=subject,
            source_id=sources[0], reason="source_edited",
        )

    assert [str(row["id"]) for row in stale] == [summary_id]
    assert (await _row(summary_id))["stale_at"] is not None


async def test_marking_stale_is_idempotent(subject):
    """Retiring several sources of one summary marks it once.

    Without `stale_at IS NULL` in the UPDATE, the second source would overwrite
    the first's timestamp — harmless here, but it would make "when did this go
    stale?" answer the wrong question, and it is the kind of drift that is
    invisible until someone relies on it.
    """
    sources, summary_id = await _seed_summary_with_sources(
        subject,
        ["The user prefers Java.", "The user prefers to receive code in Python."],
        "The user prefers Java but also likes receiving code in Python.",
    )

    first = await _delete_source(subject, sources[0], "source_deleted")
    stamped_at = (await _row(summary_id))["stale_at"]

    second = await _delete_source(subject, sources[1], "source_deleted")

    assert len(first) == 1, "the first delete should mark it"
    assert second == [], "the second must find nothing left to mark"
    assert (await _row(summary_id))["stale_at"] == stamped_at, (
        "the original staleness timestamp was overwritten"
    )


async def test_an_unconsolidated_source_marks_nothing(subject):
    """Most memories were never folded into a summary. The common path is a no-op."""
    async with session(subject, subject) as conn:
        orphan = await insert_memory(
            subject, subject, "The user's sister is Mia.", _vector(50),
            "user_statement", 0.7, 0.9, conn=conn,
        )

    stale = await _delete_source(subject, orphan, "source_deleted")
    assert stale == []


async def test_the_rebuild_retires_the_summary_and_frees_survivors(subject):
    """The lazy half: the next reflection run cleans up and reopens the sources.

    Marking stale removes the summary from retrieval immediately, which is the
    safety property — but it leaves the OTHER facts folded into it unsummarised.
    This closes that gap.
    """
    sources, summary_id = await _seed_summary_with_sources(
        subject,
        ["The user prefers Java.", "The user works at TCS."],
        "The user prefers Java and works at TCS.",
    )
    await _delete_source(subject, sources[0], "source_deleted")

    result = await rebuild_stale_summaries(subject, subject)

    assert result["retired"] == [summary_id]
    assert (await _row(summary_id))["deleted_at"] is not None, (
        "the stale summary should be soft-deleted, not left lying around"
    )

    survivor = await _row(sources[1])
    assert survivor["consolidated_at"] is None, (
        "the surviving source must be freed so the next clustering pass can "
        "rebuild a summary from what is actually left"
    )
    assert survivor["consolidated_into"] is None
    assert result["sources_freed"] == 1


async def test_the_rebuild_does_not_free_the_deleted_source(subject):
    """The source the user erased must NOT return to the candidate pool.

    Freeing it would let the next reflection run fold the deleted fact straight
    back into a fresh summary — the same bug, one night later, and harder to
    spot because the summary would be new.
    """
    sources, _ = await _seed_summary_with_sources(
        subject,
        ["The user prefers Java.", "The user works at TCS."],
        "The user prefers Java and works at TCS.",
    )
    await _delete_source(subject, sources[0], "source_deleted")

    await rebuild_stale_summaries(subject, subject)

    deleted = await _row(sources[0])
    assert deleted["deleted_at"] is not None
    assert deleted["consolidated_at"] is not None, (
        "the deleted source was un-consolidated and is now eligible to be "
        "summarised again — it must stay out"
    )


async def test_invalidation_is_audited(subject):
    """M7's trail covers it: invalidating a summary is a governed mutation."""
    sources, summary_id = await _seed_summary_with_sources(
        subject, ["The user prefers Java."], "The user prefers Java."
    )
    await _delete_source(subject, sources[0], "source_deleted")

    async with admin_session() as conn:
        cursor = await conn.execute(
            "SELECT action, memory_id, metadata FROM audit_log"
            "  WHERE subject_id = %s::uuid AND metadata->>'outcome' = 'stale'",
            (subject,),
        )
        rows = [dict(r) for r in await cursor.fetchall()]

    assert len(rows) == 1, f"expected one invalidation audit row, got {rows}"
    assert rows[0]["action"] == "update"
    assert str(rows[0]["memory_id"]) == summary_id
    assert rows[0]["metadata"]["reason"] == "source_deleted"
    assert str(rows[0]["metadata"]["triggered_by"]) == sources[0]


# ---------------------------------------------------------------------------
# the PRODUCTION wiring — the endpoints, not the helper
# ---------------------------------------------------------------------------
#
# A cold verifier deleted the cascade call from `api/memories.py` and 102
# integration and acceptance tests stayed GREEN. Every test above reaches
# `mark_summary_stale` through a local helper, so the feature's only two real
# entry points had zero coverage: the code could have been removed from
# production entirely without a single failure.
#
# These call `delete_memory` and `patch_memory` directly.


async def _identity(subject_id: str, actor_id: str | None = None):
    from api.memories import Identity

    return Identity(subject_id, actor_id or subject_id)


async def test_the_delete_endpoint_invalidates_the_summary(subject):
    """`DELETE /memories/{id}` itself, not the helper underneath it."""
    from api.memories import delete_memory

    sources, summary_id = await _seed_summary_with_sources(
        subject, ["The user prefers Java."], "PremSiva prefers Java and works at TCS."
    )

    result = await delete_memory(memory_id=sources[0], identity=await _identity(subject))
    assert result["deleted"] is True

    assert (await _row(summary_id))["stale_at"] is not None, (
        "the endpoint reported a successful erasure while the summary quoting "
        "the erased fact stayed live and retrievable"
    )


async def test_the_patch_endpoint_invalidates_the_summary(subject, monkeypatch):
    """`PATCH /memories/{id}` itself. The re-embed is stubbed: this is about the
    cascade, and the real call would spend a rate-limited provider request."""
    from llm import config as llm_config
    from api.memories import MemoryPatch, patch_memory

    sources, summary_id = await _seed_summary_with_sources(
        subject, ["The user works at TCS."], "The user works at TCS."
    )

    async def _stub_embed(texts, **kwargs):
        return [_vector(77) for _ in ([texts] if isinstance(texts, str) else texts)]

    # `patch_memory` does `from llm.config import embed` INSIDE the function, so
    # the name is resolved on the module at call time — patch it there.
    monkeypatch.setattr(llm_config, "embed", _stub_embed)

    await patch_memory(
        patch=MemoryPatch(content="The user works at Infosys."),
        memory_id=sources[0],
        identity=await _identity(subject),
    )

    assert (await _row(summary_id))["stale_at"] is not None, (
        "an edit left the summary asserting what the user had just corrected"
    )


async def test_erasure_cascades_across_actors(subject):
    """THE BLOCKER a cold verifier found: RLS silently swallowed the cascade.

    Both `memories` policies are scoped on `actor_id` as well as `subject_id`,
    so a summary written by a DIFFERENT actor — the reflection job's, or another
    client of the same subject — was invisible to the deleting caller. The
    UPDATE matched zero rows, the cascade returned `[]`, and that looked exactly
    like "this source had no summary". The endpoint returned 200 and the audit
    trail recorded a clean erasure while the fact stayed retrievable.

    The asymmetry that hid it: `ensure_owned` authorises on `subject_id` alone,
    so the delete is legitimate; the cascade was gated on `actor_id` too.
    """
    from api.memories import delete_memory

    other_actor = str(uuid.uuid4())

    async with session(subject, subject) as conn:
        source_id = await insert_memory(
            subject, subject, "The user prefers Java.", _vector(1),
            "user_preference", 0.7, 0.9, conn=conn,
        )
    # The summary belongs to the same SUBJECT but a different ACTOR, which is
    # what a background job writing on the user's behalf looks like.
    async with admin_session() as conn:
        cursor = await conn.execute(
            "INSERT INTO memories (subject_id, actor_id, content, source, importance,"
            "                      confidence)"
            " VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s) RETURNING id",
            (subject, other_actor, "PremSiva prefers Java.", REFLECTION_SOURCE, 0.6, 0.7),
        )
        summary_id = str((await cursor.fetchone())["id"])
        await conn.execute(
            "UPDATE memories SET consolidated_at = now(), consolidated_into = %s::uuid"
            " WHERE id = %s::uuid",
            (summary_id, source_id),
        )

    await delete_memory(memory_id=source_id, identity=await _identity(subject))

    assert (await _row(summary_id))["stale_at"] is not None, (
        "the summary was written by a different actor and RLS hid it from the "
        "cascade — the erasure reported success and the fact survives"
    )
