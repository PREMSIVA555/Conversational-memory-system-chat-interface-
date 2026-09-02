"""Integration tests for the reflection graph (M8 steps 7, 8, 9).

    test_reflection_writes_summary_memory
    test_reflection_summary_is_pii_filtered_and_embedded
    test_reflection_emits_audit_rows

PROVIDER BUDGET — WHY SOME OF THESE ARE STUBBED, EXPLICITLY
-----------------------------------------------------------
The Voyage account is metered at **3 requests per minute**, and a spent quota
costs 12-64 seconds of backoff per call. So:

  * the cluster's own vectors are SYNTHETIC. `_cluster_vectors()` builds
    deterministic near-parallel unit vectors with numpy — no provider call.
    Nothing under test here depends on the vectors being real: the cluster query
    asks pgvector "which of these rows are near each other", and near-parallel
    synthetic vectors answer that question exactly as well as Voyage's would,
    for free and reproducibly. Embedding-quality is M1's and M3's subject.

  * `test_reflection_writes_summary_memory` makes a REAL completion call (Groq,
    which is not the throttled provider) and a REAL embedding call. It is the
    end-to-end proof, and it costs one Voyage request.

  * the other two tests stub `llm.config.complete` and `llm.config.embed`.
    Stubbing the summarizer is not a shortcut there — it is REQUIRED. The PII
    test's whole claim is "if a summary contains PII, the stored content is
    redacted", and a live model asked to summarise memories containing an SSN
    will usually, sensibly, decline to repeat it. That would make the test pass
    without the redaction path ever running. Injecting the SSN into the model's
    output is the only way to actually exercise the guard.

Both stubs are monkeypatched on the `llm.config` MODULE, which works because
`jobs/reflection.py` and `capture/embed.py` both reach their callee through the
module rather than a `from ... import` binding. That is documented in both files
as a deliberate choice, and these tests are what it is for.

Run:  pytest tests/integration/test_reflection.py -v
"""

from __future__ import annotations

import uuid

import pytest

from graphs.reflection_graph import run_reflection
from jobs.reflection import (
    REFLECTION_SOURCE,
    SUMMARY_MAX_TOKENS,
    Cluster,
    ClusterMember,
    ReflectionProducedNothing,
    run_reflection_worker,
    select_cluster,
    summarize_cluster,
)
from llm import config as llm_config
from store.db import admin_session, session

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

EMBEDDING_DIM = 1024

#: A cluster with an unmistakable shared theme, so "the summary references the
#: cluster's theme" can be checked against concrete vocabulary rather than by
#: eyeballing. Every sentence carries at least one of THEME_WORDS.
SOURDOUGH_CLUSTER = (
    "I feed my sourdough starter with rye flour every morning before work.",
    "My sourdough loaves come out best after a cold overnight proof in the fridge.",
    "I bought a cast iron dutch oven specifically for baking sourdough bread.",
    "The bakery on Coldharbour Road sells the sourdough I am trying to copy.",
    "My sourdough starter is called Bubbles and is about four years old.",
)

THEME_WORDS = ("sourdough", "starter", "bread", "bak", "loaf", "loaves", "flour", "dough")

#: Deliberately far from the cluster in meaning, so the density query has
#: something to reject. If `select_cluster` returned these, the "densest
#: cluster" logic would be doing nothing.
DISTRACTORS = (
    "The car needs its timing belt replaced before the MOT in November.",
    "My landlord still has not fixed the skylight in the back bedroom.",
)


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------

def _cluster_vectors(n: int, *, seed: int, spread: float) -> list[list[float]]:
    """`n` deterministic unit vectors clustered `spread` away from one axis.

    `spread` controls how tight the cluster is: the vectors are one shared base
    direction plus `spread`-scaled independent noise, normalised. A small spread
    puts every pairwise cosine distance well inside
    `REFLECTION_MAX_DISTANCE`; a large one puts them outside.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    base = rng.normal(size=EMBEDDING_DIM)
    base = base / np.linalg.norm(base)
    out = []
    for _ in range(n):
        noise = rng.normal(size=EMBEDDING_DIM)
        noise = noise / np.linalg.norm(noise)
        vector = base + spread * noise
        out.append((vector / np.linalg.norm(vector)).tolist())
    return out


def _to_literal(vector) -> str:
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


async def _seed(subject_id: str, texts, vectors, *, source: str = "chat") -> list[str]:
    ids: list[str] = []
    async with admin_session() as conn:
        for text, vector in zip(texts, vectors):
            cursor = await conn.execute(
                """
                INSERT INTO memories
                       (subject_id, actor_id, content, embedding, source,
                        importance, confidence)
                VALUES (%(s)s::uuid, %(s)s::uuid, %(c)s, %(v)s::vector, %(src)s, 0.5, 0.9)
                RETURNING id
                """,
                {"s": subject_id, "c": text, "v": _to_literal(vector), "src": source},
            )
            ids.append(str((await cursor.fetchone())["id"]))
    return ids


async def _rows(subject_id: str) -> list[dict]:
    async with admin_session() as conn:
        cursor = await conn.execute(
            "SELECT id, content, source, deleted_at, consolidated_at, consolidated_into,"
            "       (embedding IS NOT NULL) AS has_embedding"
            "  FROM memories WHERE subject_id = %s::uuid ORDER BY created_at",
            (subject_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]


async def _audit(subject_id: str) -> list[dict]:
    async with admin_session() as conn:
        cursor = await conn.execute(
            "SELECT id, action, memory_id, metadata FROM audit_log"
            " WHERE subject_id = %s::uuid ORDER BY created_at",
            (subject_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]


@pytest.fixture
async def reflection_subject():
    """A fresh subject whose memories and audit rows are purged at teardown."""
    subject_id = str(uuid.uuid4())
    try:
        yield subject_id
    finally:
        async with admin_session() as conn:
            # memories first: audit_log.memory_id is ON DELETE SET NULL, and the
            # append-only trigger permits that internal UPDATE for the owner.
            await conn.execute(
                "DELETE FROM memories WHERE subject_id = %s::uuid", (subject_id,)
            )
            await conn.execute(
                "DELETE FROM audit_log WHERE subject_id = %s::uuid", (subject_id,)
            )


@pytest.fixture
async def seeded_cluster(reflection_subject):
    """The sourdough cluster plus two unrelated distractors, all embedded."""
    tight = _cluster_vectors(len(SOURDOUGH_CLUSTER), seed=17, spread=0.25)
    loose = _cluster_vectors(len(DISTRACTORS), seed=99, spread=0.25)
    cluster_ids = await _seed(reflection_subject, SOURDOUGH_CLUSTER, tight)
    distractor_ids = await _seed(reflection_subject, DISTRACTORS, loose)
    return {
        "subject_id": reflection_subject,
        "cluster_ids": cluster_ids,
        "distractor_ids": distractor_ids,
    }


@pytest.fixture
def stub_embed(monkeypatch):
    """Deterministic embeddings, no provider call. Records the calls made."""
    calls: list[list[str]] = []

    async def _embed(texts, **kwargs):
        batch = [texts] if isinstance(texts, str) else list(texts)
        calls.append(batch)
        return _cluster_vectors(len(batch), seed=4242, spread=0.25)

    monkeypatch.setattr(llm_config, "embed", _embed)
    return calls


# ---------------------------------------------------------------------------
# test_reflection_writes_summary_memory  (real LLM, real embedding)
# ---------------------------------------------------------------------------

async def test_reflection_writes_summary_memory(seeded_cluster):
    """The graph writes one `source='reflection'` memory about the cluster's theme.

    Live completion and live embedding. The theme check is deliberately
    vocabulary-based rather than an LLM judgement: the summary must contain at
    least one of the cluster's own distinctive nouns, which is the property step
    7 actually asks for ("its content references the cluster's theme") and the
    property that makes the summary findable by the same searches its sources
    are.
    """
    subject_id = seeded_cluster["subject_id"]

    # The cluster query itself, before the graph runs — so a failure downstream
    # can be told apart from "there was never a cluster".
    async with session(subject_id, subject_id) as conn:
        cluster = await select_cluster(conn, subject_id=subject_id)
    assert cluster is not None, "no cluster was found in the seeded fixture"
    assert set(cluster.ids) == set(seeded_cluster["cluster_ids"]), (
        "the cluster picked up the distractors, or missed a member: "
        f"{sorted(cluster.ids)}"
    )
    assert not (set(cluster.ids) & set(seeded_cluster["distractor_ids"]))

    state = await run_reflection(subject_id=subject_id, actor_id=subject_id)

    assert state.get("skipped") is None, f"the graph short-circuited: {state['skipped']}"
    assert state["summary_id"], "no summary memory was written"

    rows = await _rows(subject_id)
    summaries = [r for r in rows if r["source"] == REFLECTION_SOURCE]
    assert len(summaries) == 1, f"expected exactly one summary row, got {len(summaries)}"
    summary = summaries[0]

    assert str(summary["id"]) == state["summary_id"]
    assert summary["has_embedding"] is True, "the summary was written without a vector"
    assert summary["content"].strip(), "the summary is empty"

    lowered = summary["content"].lower()
    assert any(word in lowered for word in THEME_WORDS), (
        f"the summary does not reference the cluster's theme: {summary['content']!r}"
    )

    # Sources marked consolidated, distractors untouched — this is what stops
    # the next run re-summarising the same cluster.
    by_id = {str(r["id"]): r for r in rows}
    for memory_id in seeded_cluster["cluster_ids"]:
        assert by_id[memory_id]["consolidated_at"] is not None, (
            f"source {memory_id} was not marked consolidated"
        )
        assert str(by_id[memory_id]["consolidated_into"]) == state["summary_id"]
    for memory_id in seeded_cluster["distractor_ids"]:
        assert by_id[memory_id]["consolidated_at"] is None, (
            "an unrelated memory was marked consolidated"
        )

    # ASCII-escaped before printing. The model reaches for U+2011 (non-breaking
    # hyphen) and U+2019 in perfectly good summaries, and a bare print of those
    # raises UnicodeEncodeError on a cp1252 Windows console under `-s` — failing
    # a passing test for a console encoding. Same guard `evals/run_eval.py`
    # applies to its own output.
    printable = summary["content"].encode("ascii", "backslashreplace").decode("ascii")
    print(f"\n  summary written: {printable}")


async def test_a_second_run_does_not_resummarise_the_same_cluster(
    seeded_cluster, stub_embed, monkeypatch
):
    """`consolidated_at` is what makes the job converge.

    Without it the densest cluster is re-summarised every night — and each new
    summary, being about the same theme, joins the cluster and makes it denser.
    Stubbed provider calls: this test is about the exclusion predicate, not
    about summary text.
    """
    async def _complete(messages, **kwargs):
        return "The user bakes sourdough bread and keeps a rye starter."

    monkeypatch.setattr(llm_config, "complete", _complete)
    subject_id = seeded_cluster["subject_id"]

    first = await run_reflection(subject_id=subject_id, actor_id=subject_id)
    assert first["summary_id"]
    assert len(first["consolidated"]) == len(seeded_cluster["cluster_ids"])

    second = await run_reflection(subject_id=subject_id, actor_id=subject_id)

    assert second.get("summary_id") is None, (
        "a second run produced another summary of an already-consolidated cluster"
    )
    assert second.get("skipped") == "no_cluster"

    rows = await _rows(subject_id)
    assert len([r for r in rows if r["source"] == REFLECTION_SOURCE]) == 1


# ---------------------------------------------------------------------------
# test_reflection_summary_is_pii_filtered_and_embedded  (stubbed on purpose)
# ---------------------------------------------------------------------------

async def test_reflection_summary_is_pii_filtered_and_embedded(
    reflection_subject, stub_embed, monkeypatch
):
    """A summary carrying PII is stored redacted, and with a vector.

    THE SUMMARIZER IS STUBBED, AND THAT IS THE TEST. A live model asked to
    consolidate memories containing an SSN will usually decline to repeat it, so
    a "real" version of this test would pass with the redaction path never
    running — a green rectangle. Forcing the SSN into the model's output is the
    only way to prove the guard fires.

    The redaction itself is entirely real: `graphs/reflection_graph.py` runs
    `capture.pii.pii_node`, the same Presidio engine that guards the capture
    path.
    """
    ssn = "123-45-6789"
    email = "halina.novak@example.com"

    async def _complete(messages, **kwargs):
        return (
            f"The user's benefits paperwork lists the social security number {ssn} "
            f"and they receive correspondence at {email}."
        )

    monkeypatch.setattr(llm_config, "complete", _complete)

    vectors = _cluster_vectors(4, seed=31, spread=0.25)
    await _seed(
        reflection_subject,
        (
            f"My social security number is {ssn} and I keep forgetting it.",
            f"The benefits office emails me at {email} about the paperwork.",
            "The benefits paperwork has to be back with them before the deadline.",
            "I filed the benefits correspondence in the folder by the door.",
        ),
        vectors,
    )

    state = await run_reflection(
        subject_id=reflection_subject, actor_id=reflection_subject
    )
    assert state["summary_id"], f"no summary written (skipped={state.get('skipped')})"

    rows = await _rows(reflection_subject)
    summary = next(r for r in rows if r["source"] == REFLECTION_SOURCE)
    content = summary["content"]

    # -- redacted --------------------------------------------------------
    assert ssn not in content, f"the raw SSN reached the content column: {content!r}"
    assert "123456789" not in content.replace("-", "")
    assert email not in content, f"the raw email reached the content column: {content!r}"
    assert "[REDACTED_US_SSN]" in content, (
        f"the SSN was neither present nor redacted — did the pii node run? {content!r}"
    )
    assert "[REDACTED_EMAIL_ADDRESS]" in content, content

    # The node reported what it found, so a future reader can see the guard fired.
    assert "US_SSN" in state["pii_entities"]
    assert "EMAIL_ADDRESS" in state["pii_entities"]

    # -- embedded --------------------------------------------------------
    assert summary["has_embedding"] is True, "the summary has a NULL embedding"
    assert stub_embed, "the embed node never ran"
    # And it embedded the REDACTED text, not the original — the order of the
    # graph's nodes is what guarantees this, and getting it backwards would send
    # the raw SSN to a third-party embedding provider.
    embedded_text = stub_embed[-1][0]
    assert ssn not in embedded_text, (
        "the pre-redaction text was sent to the embedding provider"
    )
    assert "[REDACTED_US_SSN]" in embedded_text

    async with admin_session() as conn:
        cursor = await conn.execute(
            "SELECT vector_dims(embedding) AS dims FROM memories WHERE id = %s::uuid",
            (state["summary_id"],),
        )
        assert (await cursor.fetchone())["dims"] == EMBEDDING_DIM


# ---------------------------------------------------------------------------
# test_reflection_emits_audit_rows
# ---------------------------------------------------------------------------

async def test_reflection_emits_audit_rows(seeded_cluster, stub_embed, monkeypatch):
    """The summary write produces an `audit_log` row — exactly one, action='write'.

    Plus one `update` row per source marked consolidated, because that is also a
    mutation of the user's data. Asserted as exact counts rather than
    `>= 1`: M7's whole property is "exactly one audit row per action, not zero,
    not duplicated".
    """
    async def _complete(messages, **kwargs):
        return "The user bakes sourdough bread and keeps a rye starter called Bubbles."

    monkeypatch.setattr(llm_config, "complete", _complete)
    subject_id = seeded_cluster["subject_id"]

    before = await _audit(subject_id)
    assert before == [], "the fixture subject already had audit rows"

    state = await run_reflection(subject_id=subject_id, actor_id=subject_id)
    summary_id = state["summary_id"]
    assert summary_id

    rows = await _audit(subject_id)
    writes = [r for r in rows if r["action"] == "write"]
    updates = [r for r in rows if r["action"] == "update"]

    assert len(writes) == 1, f"expected exactly one write audit row, got {len(writes)}"
    assert str(writes[0]["memory_id"]) == summary_id
    assert writes[0]["metadata"]["job"] == "reflection"
    assert writes[0]["metadata"]["source_count"] == len(seeded_cluster["cluster_ids"])

    assert len(updates) == len(seeded_cluster["cluster_ids"]), (
        f"expected one update row per consolidated source, got {len(updates)}"
    )
    assert {str(r["memory_id"]) for r in updates} == set(seeded_cluster["cluster_ids"])
    for row in updates:
        assert row["metadata"]["consolidated_into"] == summary_id

    assert {r["action"] for r in rows} == {"write", "update"}
    assert len(rows) == 1 + len(seeded_cluster["cluster_ids"])


async def test_reflection_ignores_soft_deleted_sources(
    reflection_subject, stub_embed, monkeypatch
):
    """Step 9: cluster selection respects the soft-delete filter.

    A deleted memory must not be summarised — a summary is a new, undeleted row
    containing the erased content, which would be a hole straight through M7's
    right-to-erasure story.
    """
    async def _complete(messages, **kwargs):
        # Echo the sources so the test can see exactly what was handed over.
        return "SOURCES: " + messages[-1]["content"].replace("\n", " ")

    monkeypatch.setattr(llm_config, "complete", _complete)

    vectors = _cluster_vectors(4, seed=77, spread=0.25)
    ids = await _seed(
        reflection_subject,
        (
            "I take the number 24 bus to the pool on Saturday mornings.",
            "The pool near the station opens at six on weekdays.",
            "My swimming club subscription renews in April.",
            "ERASED SECRET the swimming club committee minutes are confidential.",
        ),
        vectors,
    )
    erased_id = ids[-1]

    async with admin_session() as conn:
        await conn.execute(
            "UPDATE memories SET deleted_at = now() WHERE id = %s::uuid", (erased_id,)
        )

    state = await run_reflection(
        subject_id=reflection_subject, actor_id=reflection_subject
    )

    assert state["summary_id"], f"no summary (skipped={state.get('skipped')})"
    assert erased_id not in state["cluster_ids"], (
        "a soft-deleted memory was selected as a reflection source"
    )
    assert erased_id not in state["consolidated"]

    rows = await _rows(reflection_subject)
    summary = next(r for r in rows if r["source"] == REFLECTION_SOURCE)
    assert "ERASED SECRET" not in summary["content"], (
        "erased content was copied into a live summary row"
    )

    erased = next(r for r in rows if str(r["id"]) == erased_id)
    assert erased["deleted_at"] is not None
    assert erased["consolidated_at"] is None


# ---------------------------------------------------------------------------
# the silent-no-op guard  (added after cold verification of M8 failed DoD 9)
# ---------------------------------------------------------------------------
#
# A cold verifier ran `python -m jobs.run --job reflection` nine times and got a
# written summary twice. The other seven runs printed
# `{"skipped": "empty_summary" | "no_cluster", "summaries_written": 0}` and
# exited **0**. Two separate defects were hiding in that one symptom, and these
# tests pin them apart:
#
#   1. the completion really was coming back empty, because gpt-oss spends its
#      budget on reasoning before emitting content and the global 1024-token
#      default left 2 tokens for the answer (measured: finish_reason=length,
#      completion_tokens=1024, reasoning_tokens=1022). Fixed by giving this
#      prompt its own budget, `SUMMARY_MAX_TOKENS`.
#
#   2. even with the budget fixed, a run that finds a cluster and writes nothing
#      exited 0 — indistinguishable, to a scheduler, from a quiet night with
#      nothing to consolidate. That is the failure mode that matters: nobody
#      reads a nightly log line that says success.
#
# Test 2 is the one that must never be deleted. Fixing the token budget makes
# the empty completion rare; it cannot make it impossible, because the model is
# free to return an empty string for reasons outside this repo's control.


async def test_barren_run_raises_instead_of_exiting_zero(
    seeded_cluster, stub_embed, monkeypatch
):
    """A run that finds a cluster and writes no summary must FAIL, loudly.

    Forces the exact observed condition — a completion that returns "" — and
    asserts the worker raises rather than returning a clean record. Returning
    `summaries_written == 0` with outcome "ok" is what made the original defect
    invisible for a whole milestone.
    """
    async def _empty(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(llm_config, "complete", _empty)

    with pytest.raises(ReflectionProducedNothing) as excinfo:
        await run_reflection_worker(
            subject_id=seeded_cluster["subject_id"],
            actor_id=seeded_cluster["subject_id"],
        )

    message = str(excinfo.value)
    assert "found a cluster but wrote no summary" in message
    assert seeded_cluster["subject_id"] in message, (
        "the error must name the subject, or an operator cannot act on it"
    )
    assert "empty_summary" in message, (
        "the error must carry WHY nothing was written, not just that nothing was"
    )


async def test_having_nothing_to_consolidate_is_success_not_failure(
    reflection_subject, stub_embed, monkeypatch
):
    """The other side of the guard: an empty corpus must still exit 0.

    Without this, the fix for the silent no-op would turn every quiet night into
    a red build — a cron that alarms when there is genuinely nothing to do gets
    muted, and a muted alarm is worse than the silence it replaced.
    """
    async def _unused(*_args, **_kwargs):
        raise AssertionError("no completion should be attempted with no cluster")

    monkeypatch.setattr(llm_config, "complete", _unused)

    record = await run_reflection_worker(
        subject_id=reflection_subject,
        actor_id=reflection_subject,
    )

    assert record.summaries_written == 0
    assert record.error is None, (
        f"an empty corpus is not an error, got {record.error!r}"
    )


async def test_summary_call_asks_for_more_tokens_than_the_global_default():
    """The summary prompt must not run on the global chat budget.

    Pins the root cause rather than the symptom. `llm/config.py`'s MAX_TOKENS
    TRAP documents that too small a budget yields an empty string rather than an
    error, and this prompt — find a shared theme across up to eight memories,
    and refuse to invent one if there isn't one — reasons far harder than a chat
    turn. If someone later "simplifies" this back to the default, the empty
    summaries return and they look like a flaky model.
    """
    assert SUMMARY_MAX_TOKENS > llm_config.default_max_tokens(), (
        f"the summary budget ({SUMMARY_MAX_TOKENS}) must exceed the global "
        f"default ({llm_config.default_max_tokens()}) — see the measured "
        f"1022-of-1024 reasoning-token exhaustion in jobs/reflection.py"
    )
    assert SUMMARY_MAX_TOKENS >= 2048


async def test_summarize_cluster_passes_the_budget_through(monkeypatch):
    """The budget is actually sent to the provider, not merely defined.

    A constant nobody passes is decoration. This asserts the call carries it,
    and that an explicit override still wins.
    """
    seen: list[int | None] = []

    async def _spy(_messages, **kwargs):
        seen.append(kwargs.get("max_tokens"))
        return "a summary sentence"

    monkeypatch.setattr(llm_config, "complete", _spy)

    cluster = Cluster(
        subject_id="s",
        seed_id="seed",
        members=tuple(
            ClusterMember(str(i), text, 0.0)
            for i, text in enumerate(["one thing", "another thing", "a third thing"])
        ),
    )

    await summarize_cluster(cluster)
    assert seen == [SUMMARY_MAX_TOKENS]

    await summarize_cluster(cluster, max_tokens=777)
    assert seen == [SUMMARY_MAX_TOKENS, 777], "an explicit budget must still win"
