"""M9 integration tests — what the assistant is allowed to remember about you.

THE LOOP THESE CLOSE, reproduced from a real user's data
--------------------------------------------------------
Capture reads BOTH halves of a turn, so the extractor can mint a memory out of
the assistant's own words. Timestamps from the incident:

    10:48:40  user deletes "The user prefers to receive code in Python."
    10:48:42  user deletes "The user prefers Java."
    10:50:21  capture writes  "The user prefers Java."                assistant_note
    10:50:21  capture writes  "The user also likes receiving code
                               examples in Python."                   assistant_note

Ninety seconds. A summary that had not yet been invalidated told the model the
user preferred Java and Python; the model said so; capture read it back out of
the reply and stored it as two brand-new LIVE rows. The fact resurrected itself,
and deleting the new rows would not have helped — it would simply have happened
again on the next turn.

THE ASYMMETRY THAT RUNS THROUGH ALL OF THIS: the user restating a fact means
they still hold it, so it must come back. The assistant restating one means only
that it read the fact somewhere. One is evidence; the other is an echo.

Run:  pytest tests/integration/test_provenance.py -v
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


def _vector(slot: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[slot % EMBEDDING_DIM] = 1.0
    return vector


def _candidate(
    text: str, *, source: str, attribute: str | None = None, slot: int = 1
) -> Candidate:
    return Candidate(
        text=text,
        source=source,
        attribute=attribute,
        embedding=_vector(slot),
        importance=0.7,
        confidence=0.9,
    )


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
                "UPDATE memories SET superseded_by = NULL, superseded_at = NULL, "
                "consolidated_into = NULL "
                "WHERE subject_id = %s::uuid",
                (subject_id,),
            )
            await conn.execute(
                "DELETE FROM memories WHERE subject_id = %s::uuid", (subject_id,)
            )


async def _rows(subject_id: str) -> list[dict]:
    async with admin_session() as conn:
        cursor = await conn.execute(
            "SELECT id, content, source, attribute, reinforcement_count,"
            "       deleted_at, superseded_at"
            "  FROM memories WHERE subject_id = %s::uuid ORDER BY created_at, id",
            (subject_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]


# ---------------------------------------------------------------------------
# rule 1 — the assistant may not set a slot
# ---------------------------------------------------------------------------

async def test_an_assistant_note_cannot_fill_a_preference_slot(subject):
    """Risk created by supersession itself, so it is closed in the same breath.

    Before slots existed, an assistant_note claiming a preference was merely
    noise. Now it would SUPERSEDE the user's real preference — the system would
    overwrite what the user said with what it had just said itself, and the user
    would have no way to tell that had happened.
    """
    results = await persist_candidates(
        subject,
        subject,
        [_candidate("The user prefers Java.", source="assistant_note",
                    attribute=LANGUAGE, slot=1)],
    )

    assert results[0]["action"] == "insert", "the fact itself is still storable"
    rows = await _rows(subject)
    assert len(rows) == 1
    assert rows[0]["attribute"] is None, (
        "an assistant_note kept its slot and could now supersede a user-stated "
        "preference"
    )


async def test_a_user_preference_keeps_its_slot(subject):
    """The control for the test above: user-sourced facts are unaffected."""
    await persist_candidates(
        subject,
        subject,
        [_candidate("The user prefers c++.", source="user_preference",
                    attribute=LANGUAGE, slot=1)],
    )
    rows = await _rows(subject)
    assert rows[0]["attribute"] == LANGUAGE


async def test_the_assistant_cannot_supersede_a_user_stated_preference(subject):
    """The two rules composed, which is where the real damage would have been."""
    await persist_candidates(
        subject, subject,
        [_candidate("The user prefers c++.", source="user_preference",
                    attribute=LANGUAGE, slot=1)],
    )
    await persist_candidates(
        subject, subject,
        [_candidate("The user prefers Java.", source="assistant_note",
                    attribute=LANGUAGE, slot=2)],
    )

    rows = await _rows(subject)
    user_row = next(r for r in rows if "c++" in r["content"])
    assert user_row["superseded_at"] is None, (
        "the assistant overwrote the user's own stated preference"
    )


# ---------------------------------------------------------------------------
# rule 2 — the assistant may not resurrect what the user removed
# ---------------------------------------------------------------------------

async def test_an_assistant_note_cannot_resurrect_a_deleted_memory(subject):
    """The incident, reproduced end to end.

    Same embedding on both candidates, which forces the similarity check to
    match — this is about provenance, not about the threshold.
    """
    shared = 10
    await persist_candidates(
        subject, subject,
        [_candidate("The user prefers Java.", source="user_preference", slot=shared)],
    )
    async with session(subject, subject) as conn:
        await conn.execute(
            "UPDATE memories SET deleted_at = now() WHERE subject_id = %s::uuid",
            (subject,),
        )

    results = await persist_candidates(
        subject, subject,
        [_candidate("The user prefers Java.", source="assistant_note", slot=shared)],
    )

    assert results[0]["action"] == "blocked", (
        f"the assistant recreated a memory the user had deleted: {results[0]}"
    )
    assert results[0]["reason"] == "assistant_resurrection"

    rows = await _rows(subject)
    assert len(rows) == 1, "no new row may be written"
    assert rows[0]["deleted_at"] is not None, "the deleted row stays deleted"


async def test_the_user_may_always_restate_a_deleted_memory(subject):
    """The other half of the asymmetry, and it matters as much as the block.

    If the user says it again, they mean it. A tombstone must never become a
    permanent ban on a fact about the user's own life — that would be a worse
    failure than the resurrection bug, and a much more confusing one.
    """
    shared = 11
    await persist_candidates(
        subject, subject,
        [_candidate("The user prefers Java.", source="user_preference", slot=shared)],
    )
    async with session(subject, subject) as conn:
        await conn.execute(
            "UPDATE memories SET deleted_at = now() WHERE subject_id = %s::uuid",
            (subject,),
        )

    results = await persist_candidates(
        subject, subject,
        [_candidate("The user prefers Java.", source="user_preference", slot=shared)],
    )

    assert results[0]["action"] == "insert", (
        f"the user restated a deleted fact and was refused: {results[0]}"
    )
    live = [r for r in await _rows(subject) if r["deleted_at"] is None]
    assert len(live) == 1


async def test_an_assistant_note_cannot_resurrect_a_superseded_preference(subject):
    """Superseded counts as removed too, for the same reason.

    Otherwise the assistant restating an old preference would reinstate it as a
    fresh live row, and — since the slot was stripped by rule 1 — it would sit
    alongside the current value rather than replacing it. Two live answers to a
    single-valued question is exactly the state M9 exists to prevent.
    """
    shared = 12
    await persist_candidates(
        subject, subject,
        [_candidate("The user prefers Java.", source="user_preference",
                    attribute=LANGUAGE, slot=shared)],
    )
    await persist_candidates(
        subject, subject,
        [_candidate("The user prefers c++.", source="user_preference",
                    attribute=LANGUAGE, slot=20)],
    )

    results = await persist_candidates(
        subject, subject,
        [_candidate("The user prefers Java.", source="assistant_note", slot=shared)],
    )

    assert results[0]["action"] == "blocked"
    live = [r for r in await _rows(subject) if r["superseded_at"] is None]
    assert len(live) == 1
    assert "c++" in live[0]["content"]


async def test_an_unrelated_assistant_note_is_still_stored(subject):
    """The block must be narrow. Genuine inferences are the reason capture reads
    the assistant's half of the turn at all — "The user has travel experience"
    came from exactly this path and is a real fact about the user.
    """
    await persist_candidates(
        subject, subject,
        [_candidate("The user prefers Java.", source="user_preference", slot=30)],
    )
    async with session(subject, subject) as conn:
        await conn.execute(
            "UPDATE memories SET deleted_at = now() WHERE subject_id = %s::uuid",
            (subject,),
        )

    results = await persist_candidates(
        subject, subject,
        [_candidate("The user has travel experience.", source="assistant_note", slot=31)],
    )

    assert results[0]["action"] == "insert", (
        "an unrelated assistant inference was blocked; the tombstone check is "
        "too broad and the system has stopped learning"
    )


# ---------------------------------------------------------------------------
# rule 3 — the assistant repeating itself is not evidence
# ---------------------------------------------------------------------------

async def test_an_assistant_note_does_not_reinforce(subject):
    """`reinforcement_count` means "the user said this again".

    It feeds M4's frequency signal, so letting the assistant increment it closed
    a loop: retrieval surfaces a fact, the model restates it, capture reinforces
    it, its frequency rises, retrieval surfaces it more. The facts that won were
    the ones the system had been talking to itself about.
    """
    shared = 40
    await persist_candidates(
        subject, subject,
        [_candidate("The user works at TCS.", source="user_statement", slot=shared)],
    )
    before = (await _rows(subject))[0]["reinforcement_count"]

    results = await persist_candidates(
        subject, subject,
        [_candidate("The user works at TCS.", source="assistant_note", slot=shared)],
    )

    assert results[0]["action"] == "ignored", f"expected no write, got {results[0]}"
    after = (await _rows(subject))[0]["reinforcement_count"]
    assert after == before, (
        f"the assistant reinforced a memory by repeating it: {before} -> {after}"
    )


async def test_the_user_repeating_a_fact_still_reinforces_it(subject):
    """The control. Rule 3 must not disable reinforcement itself."""
    shared = 41
    await persist_candidates(
        subject, subject,
        [_candidate("The user works at TCS.", source="user_statement", slot=shared)],
    )
    before = (await _rows(subject))[0]["reinforcement_count"]

    results = await persist_candidates(
        subject, subject,
        [_candidate("The user works at TCS.", source="user_statement", slot=shared)],
    )

    assert results[0]["action"] == "reinforce"
    after = (await _rows(subject))[0]["reinforcement_count"]
    assert after > before, "a genuine restatement by the user must still count"
