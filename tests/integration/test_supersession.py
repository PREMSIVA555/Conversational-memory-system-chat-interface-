"""M9 integration tests — a new value in a single-valued slot retires the old one.

THE BUG THESE PIN, reproduced from real data
--------------------------------------------
A user said they preferred Python, then Java, then C++, over two days. All three
sat in `memories` as live, mutually contradictory rows; retrieval handed the
model all three, and it answered in whichever it liked. The store was an
append-only log of everything ever said rather than a model of what is true now.

These tests drive the real `persist_candidates` against the real database, with
synthetic embeddings so nothing depends on a rate-limited provider and so dedup
never fires by accident.

Run:  pytest tests/integration/test_supersession.py -v
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from graphs.capture_state import Candidate  # noqa: E402
from store.db import admin_session, session  # noqa: E402
from store.memories import persist_candidates  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]

EMBEDDING_DIM = 1024
LANGUAGE = "default_programming_language"
FOOD = "food_preference"


def _vector(slot: int) -> list[float]:
    """A one-hot vector. Orthogonal by construction, so cosine similarity
    between any two distinct fixtures is 0 and dedup can never fire by
    accident — these tests are about supersession, not about the threshold."""
    vector = [0.0] * EMBEDDING_DIM
    vector[slot % EMBEDDING_DIM] = 1.0
    return vector


def _candidate(text: str, attribute: str | None, slot: int) -> Candidate:
    return Candidate(
        text=text,
        source="user_preference",
        attribute=attribute,
        embedding=_vector(slot),
        importance=0.7,
        confidence=0.9,
    )


@pytest.fixture
async def subject():
    """A fresh subject, purged at teardown."""
    subject_id = str(uuid.uuid4())
    try:
        yield subject_id
    finally:
        async with admin_session() as conn:
            await conn.execute(
                "DELETE FROM audit_log WHERE subject_id = %s::uuid", (subject_id,)
            )
            await conn.execute(
                "UPDATE memories SET superseded_by = NULL, superseded_at = NULL "
                "WHERE subject_id = %s::uuid",
                (subject_id,),
            )
            await conn.execute(
                "DELETE FROM memories WHERE subject_id = %s::uuid", (subject_id,)
            )


async def _rows(subject_id: str) -> list[dict]:
    async with admin_session() as conn:
        cursor = await conn.execute(
            "SELECT id, content, attribute, superseded_at, superseded_by, deleted_at"
            "  FROM memories WHERE subject_id = %s::uuid ORDER BY created_at, id",
            (subject_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def test_a_new_preference_supersedes_the_previous_one(subject):
    """Python, then Java, then C++ — only the last is left live."""
    for index, text in enumerate(
        [
            "The user prefers to receive code in Python.",
            "The user prefers Java.",
            "The user prefers c++.",
        ]
    ):
        await persist_candidates(subject, subject, [_candidate(text, LANGUAGE, index)])

    rows = await _rows(subject)
    live = [r for r in rows if r["superseded_at"] is None]

    assert len(rows) == 3, "all three statements are kept as history"
    assert len(live) == 1, f"exactly one value may be live in a single slot, got {live}"
    assert live[0]["content"] == "The user prefers c++."

    # The chain points forward, so the panel can walk "what replaced this?".
    superseded = [r for r in rows if r["superseded_at"] is not None]
    assert all(r["superseded_by"] is not None for r in superseded)
    assert str(superseded[-1]["superseded_by"]) == str(live[0]["id"])


async def test_superseded_is_not_deleted(subject):
    """Changing your mind is not a request for erasure.

    The distinction is the whole reason there are two columns. A superseded row
    leaves retrieval but stays in the curated list and the GDPR export, because
    "you told me Java in September and C++ in October" is history the user is
    entitled to see.
    """
    await persist_candidates(subject, subject, [_candidate("The user prefers Java.", LANGUAGE, 0)])
    await persist_candidates(subject, subject, [_candidate("The user prefers c++.", LANGUAGE, 1)])

    rows = await _rows(subject)
    assert all(r["deleted_at"] is None for r in rows), (
        "supersession must never set deleted_at — that would make a change of "
        "mind indistinguishable from an erasure request"
    )


async def test_a_plural_slot_never_supersedes(subject):
    """The dosa test. Both stay live.

    This is the case that rules out deciding supersession by similarity: these
    two sentences are near-identical and both true. A threshold-based rule
    deletes one of them.
    """
    await persist_candidates(subject, subject, [_candidate("The user likes dosa.", FOOD, 10)])
    await persist_candidates(subject, subject, [_candidate("The user likes idli.", FOOD, 11)])

    rows = await _rows(subject)
    assert len(rows) == 2
    assert all(r["superseded_at"] is None for r in rows), (
        "a plural slot must keep every value; superseding one of them loses a "
        "true fact about the user"
    )


async def test_slots_do_not_interfere_with_each_other(subject):
    """A language change must not disturb the food preferences."""
    await persist_candidates(subject, subject, [_candidate("The user likes dosa.", FOOD, 10)])
    await persist_candidates(subject, subject, [_candidate("The user prefers Java.", LANGUAGE, 0)])
    await persist_candidates(subject, subject, [_candidate("The user prefers c++.", LANGUAGE, 1)])

    rows = await _rows(subject)
    dosa = next(r for r in rows if "dosa" in r["content"])
    assert dosa["superseded_at"] is None, "a language change retired a food preference"

    languages = [r for r in rows if r["attribute"] == LANGUAGE]
    assert sum(1 for r in languages if r["superseded_at"] is None) == 1


async def test_a_fact_with_no_slot_supersedes_nothing(subject):
    """Most memories carry no attribute at all and must behave exactly as before."""
    await persist_candidates(subject, subject, [_candidate("The user's sister is Mia.", None, 20)])
    await persist_candidates(subject, subject, [_candidate("The user's brother is Sam.", None, 21)])

    rows = await _rows(subject)
    assert len(rows) == 2
    assert all(r["attribute"] is None for r in rows)
    assert all(r["superseded_at"] is None for r in rows)


async def test_dedup_does_not_swallow_a_change_of_mind(subject):
    """A near-duplicate filling the same single slot is a supersession.

    Left to the cosine threshold alone, a second statement similar enough to the
    first would "reinforce" it: the count would rise, the CONTENT would stay the
    OLD value, and the user's update would vanish silently.

    Measured on the rows that produced this bug, "prefers c++" vs "prefers Java"
    is 0.7735 — below the 0.82 threshold, so today they would not collide. That
    is a 0.05 margin and it is luck, not design. This test removes the
    dependence on it by making the two candidates IDENTICAL in embedding, which
    forces a dedup match, and asserting the update still wins.
    """
    same_vector = _vector(30)

    def candidate(text: str) -> Candidate:
        return Candidate(
            text=text,
            source="user_preference",
            attribute=LANGUAGE,
            embedding=same_vector,
            importance=0.7,
            confidence=0.9,
        )

    await persist_candidates(subject, subject, [candidate("The user prefers Java.")])
    results = await persist_candidates(subject, subject, [candidate("The user prefers c++.")])

    assert results[0]["action"] == "insert", (
        "a change of mind in a single-valued slot must INSERT and supersede, "
        "not reinforce the old row — reinforcing keeps the old content and the "
        f"update is lost. Got {results[0]}"
    )

    rows = await _rows(subject)
    live = [r for r in rows if r["superseded_at"] is None]
    assert len(live) == 1
    assert live[0]["content"] == "The user prefers c++."


async def test_a_repeated_identical_statement_reinforces_rather_than_churning(subject):
    """Saying the same thing twice is not a change of mind.

    The guard above must not fire on an exact restatement, or every repetition
    would create a new row and retire an identical one — churn that grows
    without bound while the assistant keeps restating preferences to itself.
    """
    same_vector = _vector(31)

    def candidate() -> Candidate:
        return Candidate(
            text="The user prefers c++.",
            source="user_preference",
            attribute=LANGUAGE,
            embedding=same_vector,
            importance=0.7,
            confidence=0.9,
        )

    await persist_candidates(subject, subject, [candidate()])
    results = await persist_candidates(subject, subject, [candidate()])

    assert results[0]["action"] == "reinforce", (
        f"an identical restatement should reinforce, not churn: {results[0]}"
    )
    rows = await _rows(subject)
    assert len(rows) == 1
    assert rows[0]["superseded_at"] is None


async def test_supersession_writes_an_audit_row(subject):
    """M7's trail covers it: a supersession is a governed mutation."""
    await persist_candidates(subject, subject, [_candidate("The user prefers Java.", LANGUAGE, 0)])
    await persist_candidates(subject, subject, [_candidate("The user prefers c++.", LANGUAGE, 1)])

    async with admin_session() as conn:
        cursor = await conn.execute(
            "SELECT action, metadata FROM audit_log"
            "  WHERE subject_id = %s::uuid AND metadata->>'outcome' = 'superseded'",
            (subject,),
        )
        rows = [dict(r) for r in await cursor.fetchall()]

    assert len(rows) == 1, f"expected exactly one supersession audit row, got {rows}"
    assert rows[0]["action"] == "update"
    assert rows[0]["metadata"]["attribute"] == LANGUAGE
    assert rows[0]["metadata"]["superseded_by"]


# ---------------------------------------------------------------------------
# the property that actually matters: it stops reaching the model
# ---------------------------------------------------------------------------
#
# Everything above proves the COLUMN is set. None of it proves the model stops
# seeing the old value, which is the entire point of the feature. These two
# drive the real retrieval paths.


async def test_a_superseded_preference_never_reaches_the_keyword_path(subject):
    """The keyword path must return only the current value.

    Chosen for this test because it needs no embedding — pure SQL over
    `content_tsv` — so it exercises the real production query with no provider
    call and no quota.
    """
    from retrieve.keyword import keyword_search
    from retrieve.types import RetrievalQuery

    await persist_candidates(subject, subject, [_candidate("The user prefers Java.", LANGUAGE, 0)])
    await persist_candidates(subject, subject, [_candidate("The user prefers c++.", LANGUAGE, 1)])

    found = await keyword_search(
        RetrievalQuery(text="prefers", subject_id=subject, actor_id=subject)
    )
    contents = [c.content for c in found]

    assert contents, "the keyword path found nothing at all — the test proves nothing"
    assert any("c++" in c for c in contents), "the current value should be retrievable"
    assert not any("Java" in c for c in contents), (
        f"the superseded preference reached retrieval and would have reached the "
        f"model: {contents}"
    )


async def test_a_superseded_preference_never_reaches_the_semantic_path(subject, monkeypatch):
    """The semantic path must return only the current value.

    The query embedding is stubbed rather than fetched: the provider is capped
    at 3 requests/minute and this assertion is about a WHERE clause, not about
    embedding quality. The stub returns the same one-hot vector the fixtures
    use, so both rows are equidistant from the query and only the filter can
    separate them — which is exactly the condition under which a missing filter
    would show up.
    """
    from retrieve import semantic as semantic_module
    from retrieve.types import RetrievalQuery

    shared = _vector(40)

    def candidate(text: str) -> Candidate:
        return Candidate(
            text=text,
            source="user_preference",
            attribute=LANGUAGE,
            embedding=shared,
            importance=0.7,
            confidence=0.9,
        )

    await persist_candidates(subject, subject, [candidate("The user prefers Java.")])
    await persist_candidates(subject, subject, [candidate("The user prefers c++.")])

    async def _stub_embed(_text: str) -> list[float]:
        return shared

    monkeypatch.setattr(semantic_module, "embed_query", _stub_embed)

    found = await semantic_module.semantic_search(
        RetrievalQuery(text="which language do I prefer", subject_id=subject, actor_id=subject)
    )
    contents = [c.content for c in found]

    assert contents, "the semantic path found nothing at all — the test proves nothing"
    assert not any("Java" in c for c in contents), (
        f"the superseded preference reached retrieval despite being equidistant "
        f"from the query — the filter is missing on this path: {contents}"
    )


async def test_the_curated_list_still_shows_superseded_memories(subject):
    """Superseded leaves RETRIEVAL, not the user's view.

    The user changed their mind; they did not ask for erasure. Hiding the
    history from the panel would make supersession indistinguishable from
    deletion, and would quietly remove the user's ability to notice — and
    correct — a supersession the extractor got wrong.
    """
    from api.memories import MEMORY_COLUMNS, serialize_memory

    await persist_candidates(subject, subject, [_candidate("The user prefers Java.", LANGUAGE, 0)])
    await persist_candidates(subject, subject, [_candidate("The user prefers c++.", LANGUAGE, 1)])

    async with admin_session() as conn:
        cursor = await conn.execute(
            f"SELECT {MEMORY_COLUMNS} FROM memories"
            f" WHERE subject_id = %s::uuid AND deleted_at IS NULL ORDER BY created_at",
            (subject,),
        )
        payloads = [serialize_memory(dict(row)) for row in await cursor.fetchall()]

    assert len(payloads) == 2, "both values must remain visible to the user"

    old = next(p for p in payloads if "Java" in p["content"])
    new = next(p for p in payloads if "c++" in p["content"])

    assert old["superseded"] is True
    assert old["superseded_at"] is not None
    assert old["superseded_by"] == new["id"], "the panel can follow what replaced it"
    assert old["attribute"] == LANGUAGE

    assert new["superseded"] is False
    assert new["superseded_by"] is None


async def test_a_superseded_row_cannot_absorb_a_new_statement(subject):
    """`find_similar` must not offer retired rows as dedup targets.

    UNDEFENDED UNTIL A COLD VERIFIER REMOVED THE FILTER AND NOTHING WENT RED.
    The commit that added it called it out explicitly — "absorbing 'prefers C++'
    into the retired 'prefers Java' row would undo the supersession" — and then
    shipped no test for it.

    The verifier's measured consequence, for "Java, then C++, then Java again":

        with the filter     action=insert     live=['The user prefers Java.']
        without the filter  action=reinforce  live=['The user prefers c++.']

    Without it the user's newest statement is silently discarded onto a row that
    is no longer live, and the value they just moved away from stays current.
    Nothing errors and nothing in the panel explains it.
    """
    shared = _vector(60)

    def candidate(text: str) -> Candidate:
        return Candidate(
            text=text,
            source="user_preference",
            attribute=LANGUAGE,
            embedding=shared,
            importance=0.7,
            confidence=0.9,
        )

    await persist_candidates(subject, subject, [candidate("The user prefers Java.")])
    await persist_candidates(subject, subject, [candidate("The user prefers c++.")])

    # ...and now back to Java. Identical embedding, so the ONLY thing that can
    # stop the retired Java row absorbing this is the filter in `find_similar`.
    results = await persist_candidates(subject, subject, [candidate("The user prefers Java.")])

    assert results[0]["action"] == "insert", (
        "the new statement was absorbed by a superseded row: "
        f"{results[0]}"
    )

    live = [r for r in await _rows(subject) if r["superseded_at"] is None]
    assert len(live) == 1, f"exactly one value may be live: {live}"
    assert live[0]["content"] == "The user prefers Java.", (
        "the user switched back to Java and the store still says c++"
    )


async def test_superseding_a_source_invalidates_its_summary(subject):
    """The third of item 3's three triggers, which had no test.

    Delete and edit were covered; supersession was not, even though it is the
    one that fires without any explicit user action on the summary's source.
    """
    from jobs.reflection import REFLECTION_SOURCE
    from store.memories import insert_memory

    async with session(subject, subject) as conn:
        source_id = await insert_memory(
            subject, subject, "The user prefers Java.", _vector(70),
            "user_preference", 0.7, 0.9, attribute=LANGUAGE, conn=conn,
        )
        summary_id = await insert_memory(
            subject, subject, "PremSiva prefers Java.", _vector(71),
            REFLECTION_SOURCE, 0.6, 0.7, conn=conn,
        )
        await conn.execute(
            "UPDATE memories SET consolidated_at = now(), consolidated_into = %s::uuid"
            " WHERE id = %s::uuid",
            (summary_id, source_id),
        )

    await persist_candidates(
        subject, subject, [_candidate("The user prefers c++.", LANGUAGE, 72)]
    )

    async with admin_session() as conn:
        cursor = await conn.execute(
            "SELECT stale_at FROM memories WHERE id = %s::uuid", (summary_id,)
        )
        stale_at = (await cursor.fetchone())["stale_at"]

    assert stale_at is not None, (
        "superseding a source left the summary built on it live, still asserting "
        "a preference the user has replaced"
    )
