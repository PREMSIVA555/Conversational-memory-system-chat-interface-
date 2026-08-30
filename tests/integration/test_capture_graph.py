"""M2 integration tests -- the capture graph against the real stack.

Real everything: live Groq extraction/scoring, live Voyage embeddings, real
Presidio, real Postgres with RLS enforcing, real advisory-lock concurrency
control. Nothing is stubbed except where a test says so and explains why.

Read-back assertions go through `store.rows()`, which uses the owning superuser
and therefore bypasses RLS -- see `conftest.py`. A test asserting "no raw SSN is
in the content column" must be able to see every row, or a leak into a row RLS
happens to hide would pass silently.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from capture import config as capture_config
from capture import extract as extract_module
from capture import worker as worker_module
from graphs.capture_graph import run_capture
from graphs.capture_state import Candidate
from llm import config as llm_config

#: A hang must surface as a FAILURE, never as an indefinite stall that blocks
#: the whole suite. The bound is deliberately large -- a single capture run can
#: legitimately sit out several provider rate-limit windows (the configured
#: embedding key is throttled to 3 requests/minute), and a test doing two
#: captures can therefore take many minutes of *correct* waiting. This is a
#: liveness backstop, not a performance assertion. pytest-timeout uses its
#: thread method on Windows and dumps every thread's stack on expiry, so a real
#: deadlock names its own line.
pytestmark = [pytest.mark.integration, pytest.mark.timeout(900)]


ASSISTANT_ACK = "Got it, I'll remember that."


# ---------------------------------------------------------------------------
# 1. capture writes a memory, asynchronously
# ---------------------------------------------------------------------------


async def test_capture_writes_memory_async(chat_client, capture_worker, store, subject_id):
    """POST a memorable turn; a complete `memories` row appears within the bound."""
    response = await chat_client.post(
        "/chat",
        json={
            "message": "I'm allergic to peanuts, and I work as a nurse in Lisbon.",
            "subject_id": subject_id,
            "actor_id": subject_id,
        },
    )

    assert response.status_code == 200
    assert response.text.strip(), "the chat endpoint must return a non-empty reply"
    assert response.headers["X-Subject-Id"] == subject_id

    # The row must NOT exist yet at the moment the response completed: capture
    # is queued, not performed, by the request. (See also
    # test_capture_does_not_block_response.)
    immediately = await store.rows(subject_id)
    assert immediately == [], (
        "a memory row existed the instant the response finished -- capture ran "
        "on the request path, not off it"
    )

    rows = await store.poll_for_rows(subject_id, minimum=1, timeout=600.0)

    assert rows, "no memory row appeared within the 600s bound"
    for row in rows:
        assert row["content"], "content must not be empty"
        assert row["source"] is not None, "source must be non-null"
        assert row["importance"] is not None, "importance must be non-null"
        assert row["confidence"] is not None, "confidence must be non-null"
        assert 0.0 <= row["importance"] <= 1.0
        assert 0.0 <= row["confidence"] <= 1.0
        assert row["has_embedding"], "embedding must be populated for M3 retrieval"
        assert str(row["subject_id"]) == subject_id
        assert row["reinforcement_count"] == 0
        assert row["deleted_at"] is None


# ---------------------------------------------------------------------------
# 2. capture is genuinely off the request path
# ---------------------------------------------------------------------------


async def test_capture_does_not_block_response(
    chat_client, capture_worker, store, subject_id, monkeypatch
):
    """With capture artificially slowed to 5s, the reply still returns in well under 1s.

    The reply generation is stubbed to a constant so the measurement isolates
    the capture path -- otherwise provider latency would dominate and the test
    would prove nothing either way.

    The capture graph is then wrapped in a 5-second delay. If capture were
    awaited anywhere on the request path -- even with a short timeout -- the
    response could not possibly complete in under a second. The final drain
    proves capture really ran afterwards rather than being skipped.
    """
    capture_delay = 5.0
    latency_budget = 1.0

    async def instant_reply(messages, **kwargs):
        return "Understood."

    monkeypatch.setattr(llm_config, "complete", instant_reply)

    started_capture = asyncio.Event()
    finished_capture = asyncio.Event()

    async def slow_capture(subject, actor, turn, **kwargs):
        started_capture.set()
        await asyncio.sleep(capture_delay)
        finished_capture.set()
        return {"write_results": [{"action": "insert", "memory_id": "stub"}]}

    monkeypatch.setattr(worker_module, "run_capture", slow_capture)

    started = time.perf_counter()
    response = await chat_client.post(
        "/chat",
        json={
            "message": "I ride a red bicycle to work every day.",
            "subject_id": subject_id,
            "actor_id": subject_id,
        },
    )
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert response.text == "Understood."
    assert elapsed < latency_budget, (
        f"response took {elapsed:.2f}s with a {capture_delay}s capture delay -- "
        "capture is on the critical path"
    )
    assert not finished_capture.is_set(), "capture completed before the response returned"

    # ...and capture really does happen, just later.
    drained = await capture_worker.drain(timeout=capture_delay + 15.0)
    assert drained, "the capture job never drained"
    assert started_capture.is_set() and finished_capture.is_set()
    assert capture_worker.completed[-1]["status"] == "completed"


# ---------------------------------------------------------------------------
# 3 & 4. PII is redacted BEFORE persistence
# ---------------------------------------------------------------------------


def _fixed_extraction(monkeypatch, text: str, source: str = "user_statement"):
    """Pin the extractor's output so the PII-bearing string is guaranteed to
    reach the PII node.

    Only `extract` is stubbed. Presidio, the scorer, the embedder, dedup, the
    RLS session and the INSERT are all the real thing -- the assertion under
    test is "PII cannot reach the content column", and letting a live extractor
    decide whether to echo an SSN would make the test prove nothing on the runs
    where it chose to drop it.
    """

    async def stub(turn, **kwargs):
        return [Candidate(text=text, source=source)]

    monkeypatch.setattr(extract_module, "extract_candidates", stub)


async def test_pii_ssn_is_redacted_before_persistence(store, subject_id, monkeypatch):
    """A fake SSN never reaches the `content` column; a placeholder does."""
    ssn = "123-45-6789"
    digits = ssn.replace("-", "")
    fact = f"The user's social security number is {ssn}."
    _fixed_extraction(monkeypatch, fact)

    final = await run_capture(
        subject_id, subject_id, {"user": f"My SSN is {ssn}.", "assistant": ASSISTANT_ACK}
    )

    # The PII genuinely entered the pipeline...
    assert ssn in final["candidates"][0].text
    # ...and was gone by the time the pipeline could persist anything.
    assert ssn not in final["redacted"][0].text
    assert "US_SSN" in final["redacted"][0].pii_entities

    rows = await store.rows(subject_id)
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    content = rows[0]["content"]

    assert ssn not in content, f"raw SSN was persisted: {content!r}"
    assert digits not in content, f"unpunctuated SSN digits were persisted: {content!r}"
    assert "[REDACTED_US_SSN]" in content, f"no redaction placeholder in {content!r}"

    # Nothing anywhere in the table for this subject carries the digits.
    assert all(digits not in c.replace("-", "") for c in await store.contents(subject_id))


async def test_pii_email_is_redacted_before_persistence(store, subject_id, monkeypatch):
    """A fake email address never reaches the `content` column."""
    email = "jordan.reyes@example.com"
    fact = f"The user's email address is {email}."
    _fixed_extraction(monkeypatch, fact)

    final = await run_capture(
        subject_id, subject_id, {"user": f"My email is {email}.", "assistant": ASSISTANT_ACK}
    )

    assert email in final["candidates"][0].text
    assert email not in final["redacted"][0].text
    assert "EMAIL_ADDRESS" in final["redacted"][0].pii_entities

    rows = await store.rows(subject_id)
    assert len(rows) == 1
    content = rows[0]["content"]

    assert email not in content
    assert "@" not in content, f"an @-bearing address survived into {content!r}"
    assert "[REDACTED_EMAIL_ADDRESS]" in content


# ---------------------------------------------------------------------------
# 5 & 6. dedup behaviour
# ---------------------------------------------------------------------------


async def test_duplicate_fact_reinforces_single_row(store, subject_id):
    """The same fact in different words yields ONE row with an incremented count."""
    first = await run_capture(
        subject_id,
        subject_id,
        {"user": "I'm allergic to peanuts.", "assistant": ASSISTANT_ACK},
    )
    assert first["write_results"], "the first turn produced no write at all"
    assert all(r["action"] == "insert" for r in first["write_results"])

    rows_after_first = await store.rows(subject_id)
    assert len(rows_after_first) == 1, (
        f"expected one row after the first turn, got {[r['content'] for r in rows_after_first]}"
    )
    assert rows_after_first[0]["reinforcement_count"] == 0

    second = await run_capture(
        subject_id,
        subject_id,
        {
            "user": "Just so you know, peanuts give me an allergic reaction.",
            "assistant": ASSISTANT_ACK,
        },
    )

    assert [r["action"] for r in second["write_results"]] == ["reinforce"], (
        f"second turn should reinforce, not insert: {second['write_results']}"
    )

    rows = await store.rows(subject_id)
    assert len(rows) == 1, (
        f"reworded duplicate created a second row: {[r['content'] for r in rows]}"
    )
    assert rows[0]["id"] == rows_after_first[0]["id"], "reinforcement must hit the same row"
    assert rows[0]["reinforcement_count"] == 1, "reinforcement_count did not increment"
    assert rows[0]["weight"] > rows_after_first[0]["weight"], "weight did not increase"
    assert rows[0]["updated_at"] >= rows_after_first[0]["updated_at"]


async def test_distinct_facts_create_separate_rows(store, subject_id):
    """Two unrelated facts stay two rows -- the threshold is not over-aggressive."""
    await run_capture(
        subject_id,
        subject_id,
        {"user": "I'm allergic to peanuts.", "assistant": ASSISTANT_ACK},
    )
    await run_capture(
        subject_id,
        subject_id,
        {"user": "I drive a 2012 Subaru Outback.", "assistant": ASSISTANT_ACK},
    )

    rows = await store.rows(subject_id)
    contents = " ".join(r["content"].lower() for r in rows)

    assert len(rows) == 2, f"expected two distinct rows, got {[r['content'] for r in rows]}"
    assert all(r["reinforcement_count"] == 0 for r in rows), "distinct facts must not reinforce"
    assert "peanut" in contents and "subaru" in contents


# ---------------------------------------------------------------------------
# 7. dedup is scoped to the subject (auth boundary)
# ---------------------------------------------------------------------------


async def test_dedup_scoped_to_subject_id(store):
    """Subject B's identical fact gets its OWN row; subject A's is untouched."""
    subject_a = store.new_subject()
    subject_b = store.new_subject()
    turn = {"user": "I'm allergic to peanuts.", "assistant": ASSISTANT_ACK}

    await run_capture(subject_a, subject_a, turn)
    rows_a_before = await store.rows(subject_a)
    assert len(rows_a_before) == 1
    assert rows_a_before[0]["reinforcement_count"] == 0

    result_b = await run_capture(subject_b, subject_b, turn)

    assert [r["action"] for r in result_b["write_results"]] == ["insert"], (
        "subject B's fact must be a new insert, not a cross-subject reinforcement"
    )

    rows_b = await store.rows(subject_b)
    assert len(rows_b) == 1, "subject B must get their own row"
    assert rows_b[0]["id"] != rows_a_before[0]["id"]
    assert str(rows_b[0]["subject_id"]) == subject_b
    assert rows_b[0]["reinforcement_count"] == 0

    rows_a_after = await store.rows(subject_a)
    assert len(rows_a_after) == 1
    assert rows_a_after[0]["reinforcement_count"] == 0, (
        "subject A's reinforcement_count was touched by subject B's capture"
    )
    assert rows_a_after[0]["updated_at"] == rows_a_before[0]["updated_at"]


# ---------------------------------------------------------------------------
# 8. concurrency
# ---------------------------------------------------------------------------


async def test_concurrent_identical_turns_do_not_double_write(store, subject_id):
    """Two identical turns fired at once end as ONE row, not two.

    Both graph runs execute concurrently and overlap in the dedup/write window.
    Correctness comes from the transaction-scoped advisory lock in
    `store/memories.py:persist_candidates` -- the second transaction blocks
    until the first commits, then sees the row it would otherwise have
    duplicated. A read-then-write implementation fails here.
    """
    turn = {"user": "My favourite colour is teal.", "assistant": ASSISTANT_ACK}

    first, second = await asyncio.gather(
        run_capture(subject_id, subject_id, turn),
        run_capture(subject_id, subject_id, turn),
    )

    results = list(first["write_results"]) + list(second["write_results"])
    inserts = [r for r in results if r["action"] == "insert"]
    reinforces = [r for r in results if r["action"] == "reinforce"]

    rows = await store.rows(subject_id)

    assert len(rows) == 1, (
        f"concurrent identical turns produced {len(rows)} rows: "
        f"{[r['content'] for r in rows]}"
    )
    assert len(inserts) == 1, f"exactly one insert expected, got {len(inserts)}"
    assert len(reinforces) == 1, f"exactly one reinforcement expected, got {len(reinforces)}"
    assert inserts[0]["memory_id"] == reinforces[0]["memory_id"], (
        "the reinforcement must target the row the other run inserted"
    )
    assert rows[0]["reinforcement_count"] == 1


async def test_worker_pool_concurrent_identical_turns_write_one_row(
    capture_worker, store, subject_id
):
    """The same single-row outcome, driven through the REAL production path.

    The test above proves the advisory lock by calling `run_capture` directly.
    That skips the queue -- but `CAPTURE_WORKER_CONCURRENCY` consumers pulling
    from that queue is what actually runs in production, and is the reason the
    lock has to exist at all. This closes the gap: two identical jobs are
    `enqueue()`d back to back, picked up by different consumers, and raced
    through dedup/write for real.

    `enqueue()` is synchronous and non-blocking, so both jobs are on the queue
    before either consumer can finish -- the overlap is structural, not a lucky
    interleaving.
    """
    assert capture_config.worker_concurrency() > 1, (
        "this test is meaningless with a single consumer -- the jobs would "
        f"serialise (concurrency={capture_config.worker_concurrency()})"
    )

    turn = {"user": "My favourite colour is teal.", "assistant": ASSISTANT_ACK}

    job_a = capture_worker.enqueue(subject_id, subject_id, turn)
    job_b = capture_worker.enqueue(subject_id, subject_id, turn)
    assert job_a != job_b

    drained = await capture_worker.drain(timeout=900.0)
    assert drained, "the capture queue did not drain within the bound"

    records = [r for r in capture_worker.completed if r["subject_id"] == subject_id]
    assert len(records) == 2, f"expected two completed jobs, got {records}"
    assert all(r["status"] == "completed" for r in records), (
        f"a capture job did not complete cleanly: {records}"
    )

    # Both jobs went through the real write path, and the lock arbitrated them.
    actions = [w["action"] for r in records for w in r["write_results"]]
    assert sorted(actions) == ["insert", "reinforce"], (
        f"expected exactly one insert and one reinforcement, got {actions}"
    )

    rows = await store.rows(subject_id)
    assert len(rows) == 1, (
        f"two queued identical turns produced {len(rows)} rows: "
        f"{[r['content'] for r in rows]}"
    )
    assert rows[0]["reinforcement_count"] == 1
