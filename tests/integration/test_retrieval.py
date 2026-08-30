"""M3 integration tests — hybrid retrieval against the live stack.

These run against the real Postgres from M1's compose stack, as the real
non-superuser `memory_app` role, with real RLS policies enforcing. Mocking the
database here would defeat the point: the semantic path is an HNSW index scan
and the keyword path is a GIN index scan, and neither exists outside Postgres.

FIXTURES LIVE IN THIS MODULE ON PURPOSE
---------------------------------------
`tests/integration/conftest.py` belongs to M2 and is being written in parallel,
so nothing here depends on it. Everything this module needs is defined below.

TWO ENVIRONMENT FACTS THAT SHAPE THIS FILE
------------------------------------------
1. psycopg's async pool is bound to the event loop that opened it, and
   pytest-asyncio gives every test a fresh loop
   (`asyncio_default_fixture_loop_scope = "function"`). A pool cached in
   `store.db._pools` by one test would be used from a different loop by the
   next, which fails in confusing ways. `_close_pools_between_tests` below
   disposes of them at each test's teardown.

2. The Voyage account is metered at 3 requests/minute. Every query text used
   here is either already in the fixture embedding cache or is routed to a path
   that needs no embedding at all, so the whole module costs zero provider
   requests on a warm cache.

Run:  pytest tests/integration/test_retrieval.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault(
    "RETRIEVE_EMBED_CACHE", str(ROOT / "evals" / "fixtures" / "query_embedding_cache.json")
)

from evals.fixtures import seed_memories  # noqa: E402
from evals.fixtures.seed_memories import (  # noqa: E402
    GOLDEN_SET_ACTOR_ID,
    GOLDEN_SET_SUBJECT_ID,
    BY_SLUG,
    seed,
)
from evals.separation import (  # noqa: E402
    MIN_SEPARATION_MARGIN,
    MIN_SEPARATION_RANK,
    measure_separation,
)
from retrieve import config as retrieve_config  # noqa: E402
from retrieve.hybrid import hybrid_search  # noqa: E402
from retrieve.keyword import keyword_search  # noqa: E402
from retrieve.semantic import semantic_search, warm_query_cache  # noqa: E402
from retrieve.types import KEYWORD, SEMANTIC, RetrievalQuery  # noqa: E402
from store.db import close_pools, get_pool, session  # noqa: E402

pytestmark = pytest.mark.integration

GOLDEN_SET_PATH = ROOT / "evals" / "golden_set.jsonl"

def golden_record(query_id: str) -> dict:
    for line in GOLDEN_SET_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            if record["query_id"] == query_id:
                return record
    raise AssertionError(f"golden set has no record {query_id!r}")


# Query strings are READ FROM THE GOLDEN SET, never restated here.
#
# An earlier version hardcoded them as literals and they drifted: gs-002's query
# changed from "organ" to "origin", the constant did not, and the warm-up
# fixture below went on priming a query no test used. On a shared warm cache
# that was invisible, because a previous `run_eval` had already cached the real
# query. On a cold cache it surfaced as the semantic path TIMING OUT — the live
# embedding round-trip happened inside `asyncio.wait_for(PATH_TIMEOUT_MS)`
# instead of being warmed ahead of it. Deriving the strings removes the drift.
QUERY_FOODS = golden_record("gs-001")["query"]
QUERY_KEYWORD_ONLY = golden_record("gs-002")["query"]
QUERY_BOTH = golden_record("gs-003")["query"]
ALL_QUERIES = [QUERY_FOODS, QUERY_KEYWORD_ONLY, QUERY_BOTH]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def seeded_corpus():
    """Seed the golden corpus once for the module, in its own event loop.

    Synchronous on purpose: it owns its loop end to end via `asyncio.run` and
    closes the pools it opened before returning, so it never leaves a pool bound
    to a loop that later tests do not have.
    """

    async def _run():
        try:
            summary = await seed()
            await warm_query_cache(ALL_QUERIES)
            return summary
        finally:
            await close_pools()

    return asyncio.run(_run())


@pytest.fixture(autouse=True)
async def _close_pools_between_tests():
    """Open one pool up front, dispose of it at teardown.

    THE PRE-OPEN IS A WORKAROUND FOR A BUG IN `store.db.get_pool()`, which is
    M1's file and not mine to change. It does check-then-act across an await:

        pool = _pools.get(dsn)
        if pool is None or pool.closed:
            pool = AsyncConnectionPool(...)
            await pool.open(...)      # <-- both callers reach here
            _pools[dsn] = pool        # <-- last writer wins

    `retrieve.hybrid` fans its two paths out with `asyncio.gather`, and on a
    cold cache both of them miss, both construct a pool, and both open it. Only
    the last lands in `_pools`; the other is orphaned holding its connections,
    invisible to `close_pools()`, and leaks for the life of the process. It
    surfaces as a wall of "Task was destroyed but it is pending" at exit.
    Opening the pool once here means it already exists when the paths race.

    Measured: `asyncio.gather(touch(), touch())` on a cold cache creates 2 pools
    and tracks 1. See the report accompanying this milestone.

    THE TEARDOWN handles a second, unrelated hazard: psycopg's async pool is
    bound to the loop that opened it, and pytest-asyncio gives every test a
    fresh loop (`asyncio_default_fixture_loop_scope = "function"`), so a pool
    cached by one test would be used from another test's loop.

    The trailing `sleep(0)` is not superstition: `AsyncConnectionPool.close()`
    signals its worker tasks to stop but returns before they have actually been
    resumed and retired. If pytest-asyncio then closes the loop, those workers
    are garbage-collected mid-await and Python prints an "Exception ignored in:
    <coroutine object AsyncConnectionPool.worker>  RuntimeError: Event loop is
    closed" for each one. Yielding to the loop once lets them exit cleanly, so
    the suite's stderr stays readable and a real error stays visible.
    """
    await get_pool()
    yield
    await close_pools()
    await asyncio.sleep(0)


@pytest.fixture
async def fresh_subject():
    """A subject id no other test uses, cleaned up afterwards.

    The cleanup matters because `memories` is shared with M2's capture tests and
    with whatever the developer has been doing by hand. Rows left behind under a
    random subject are invisible to everyone else (RLS scopes reads to the
    subject), so they would never break a test — they would just accumulate
    silently in the table forever. Deleting them keeps repeated runs from
    growing the corpus the HNSW index has to cover.
    """
    subject_id = str(uuid.uuid4())
    yield subject_id
    async with session(subject_id, subject_id) as conn:
        await conn.execute("DELETE FROM memories WHERE subject_id = %s::uuid", (subject_id,))


async def insert_memory(
    subject_id: str, content: str, *, vector: list[float] | None = None
) -> str:
    """Insert one row for `subject_id`, returning its id.

    `vector=None` leaves `embedding` NULL, which the semantic path filters out —
    useful when a test only cares about the keyword path and wants to spend no
    embedding request.
    """
    memory_id = str(uuid.uuid4())
    async with session(subject_id, subject_id) as conn:
        await conn.execute(
            "INSERT INTO memories (id, subject_id, actor_id, content, embedding, source) "
            "VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s::vector, 'test_fixture')",
            (
                memory_id,
                subject_id,
                subject_id,
                content,
                seed_memories.to_vector_literal(vector) if vector else None,
            ),
        )
    return memory_id


async def cached_vector(text: str) -> list[float]:
    """A corpus vector straight from the fixture cache — no provider request."""
    return (await seed_memories.embeddings_for([text]))[0]


async def soft_delete(subject_id: str, memory_id: str) -> None:
    async with session(subject_id, subject_id) as conn:
        await conn.execute(
            "UPDATE memories SET deleted_at = now() WHERE id = %s::uuid", (memory_id,)
        )


def query_for(text: str, subject_id: str = GOLDEN_SET_SUBJECT_ID) -> RetrievalQuery:
    actor = GOLDEN_SET_ACTOR_ID if subject_id == GOLDEN_SET_SUBJECT_ID else subject_id
    return RetrievalQuery(text=text, subject_id=subject_id, actor_id=actor)


# ---------------------------------------------------------------------------
# 1. the semantic path returns results
# ---------------------------------------------------------------------------

async def test_semantic_path_returns_results():
    """A paraphrase with no shared content words still finds the right rows."""
    candidates = await semantic_search(query_for(QUERY_FOODS))

    assert candidates, "semantic path returned nothing against a seeded corpus"
    assert all(c.path == SEMANTIC and c.paths == {SEMANTIC} for c in candidates)

    returned = {c.memory_id for c in candidates}
    assert BY_SLUG["lactose"].id in returned, "the lactose row is the paraphrase's target"
    # Real cosine similarity, not a placeholder.
    assert all(-1.0 <= c.raw_path_scores[SEMANTIC] <= 1.0 for c in candidates)
    assert "cosine_distance" in candidates[0].metadata


# ---------------------------------------------------------------------------
# 2. the keyword path returns results
# ---------------------------------------------------------------------------

async def test_keyword_path_returns_results(fresh_subject):
    """A rare literal token is found by the tsvector path."""
    token = f"Zbigniewicz{uuid.uuid4().hex[:6]}"
    memory_id = await insert_memory(
        fresh_subject, f"Priya named her tortoise {token} after her great-uncle."
    )

    candidates = await keyword_search(query_for(token, fresh_subject))

    assert candidates, f"keyword path found nothing for the literal token {token!r}"
    assert [c.memory_id for c in candidates] == [memory_id]
    assert candidates[0].path == KEYWORD and candidates[0].paths == {KEYWORD}
    assert candidates[0].raw_path_scores[KEYWORD] > 0, "ts_rank should be positive on a match"


# ---------------------------------------------------------------------------
# 3. the keyword-only golden query — the semantic path must genuinely MISS it
# ---------------------------------------------------------------------------

async def test_keyword_only_fixture_query_returns_results():
    """gs-002 ('origin'): only the keyword path can reach the target row.

    The assertion that matters is the negative one. If the semantic path also
    returned the target, the query would be matchable both ways and would prove
    nothing about the keyword path being necessary. See the `tiles` row's
    comment in evals/fixtures/seed_memories.py for the five designs this
    replaced.
    """
    record = golden_record("gs-002")
    assert record["path_expectation"] == "keyword_only"
    target = record["expected_memory_ids"][0]

    result = await hybrid_search(query_for(record["query"]))
    assert result.candidates, "keyword-only golden query returned nothing at all"
    assert not result.degraded, result.degraded

    by_id = {c.memory_id: c for c in result.candidates}
    assert target in by_id, "the keyword-only target was not retrieved"
    assert by_id[target].paths == {KEYWORD}, (
        f"target was contributed by {sorted(by_id[target].paths)}; for this query to be a "
        "meaningful keyword-only probe the semantic path must NOT reach it"
    )

    semantic_ids = {c.memory_id for c in await semantic_search(query_for(record["query"]))}
    assert target not in semantic_ids, (
        "the semantic path returned the keyword-only target — the golden set's "
        "keyword-only case has stopped being keyword-only and must be redesigned"
    )
    assert result.path_counts[KEYWORD] >= 1


async def test_keyword_only_probe_has_a_robust_semantic_margin():
    """The probe must miss by a MARGIN, not by a hair. This is the real guard.

    Membership at one k — which the test above asserts — cannot tell a 0.0013
    margin from a 0.13 one. The first version of this probe passed that check
    while sitting 0.00128 cosine outside the cut, which is smaller than
    voyage-3.5's own ~1e-3 run-to-run drift: the assertion was green and the
    property was a coin flip. So this test asserts the property directly, over
    the WHOLE corpus with no LIMIT, and will fail on erosion long before the
    membership check flips.
    """
    record = golden_record("gs-002")
    report = await measure_separation(
        record["query"],
        record["expected_memory_ids"],
        subject_id=GOLDEN_SET_SUBJECT_ID,
        actor_id=GOLDEN_SET_ACTOR_ID,
        boundary_rank=retrieve_config.semantic_top_k(),
    )

    assert report.target_rank is not None, "the keyword-only target is not in the corpus"
    assert report.is_outside, report.summary()

    assert report.target_rank >= MIN_SEPARATION_RANK, (
        f"keyword-only target has drifted UP the semantic ranking to "
        f"{report.summary()}. The probe only proves something while the semantic "
        f"path misses it comfortably; a corpus or query edit has eroded that. "
        f"Re-derive the design (see the `tiles` row comment) rather than lowering "
        f"MIN_SEPARATION_RANK."
    )
    assert report.margin >= MIN_SEPARATION_MARGIN, (
        f"keyword-only separation margin has eroded to {report.margin:+.5f} "
        f"({report.summary()}). The floor is {MIN_SEPARATION_MARGIN}, roughly 50x "
        f"the embedding provider's ~1e-3 run-to-run drift. Below that the probe is "
        f"a coin flip, which is exactly the defect this floor exists to catch."
    )

    # Still outside at a much larger k, so a plausible SEMANTIC_TOP_K increase
    # in M4/M5 cannot quietly break the golden set.
    at_ten = await measure_separation(
        record["query"],
        record["expected_memory_ids"],
        subject_id=GOLDEN_SET_SUBJECT_ID,
        actor_id=GOLDEN_SET_ACTOR_ID,
        boundary_rank=10,
    )
    assert at_ten.is_outside, f"target enters the top 10: {at_ten.summary()}"
    assert at_ten.margin >= MIN_SEPARATION_MARGIN, at_ten.summary()


# ---------------------------------------------------------------------------
# 4. the semantic-only golden query — the keyword path must genuinely MISS it
# ---------------------------------------------------------------------------

async def test_semantic_only_fixture_query_returns_results():
    """gs-001: a paraphrase sharing no content word with its targets."""
    record = golden_record("gs-001")
    assert record["path_expectation"] == "semantic_only"
    targets = set(record["expected_memory_ids"])

    result = await hybrid_search(query_for(record["query"]))
    assert result.candidates, "semantic-only golden query returned nothing at all"
    assert not result.degraded, result.degraded

    by_id = {c.memory_id: c for c in result.candidates}
    assert targets <= set(by_id), "the semantic-only targets were not all retrieved"
    for target in targets:
        assert SEMANTIC in by_id[target].paths
        assert KEYWORD not in by_id[target].paths

    assert result.path_counts[KEYWORD] == 0, (
        "the keyword path matched something for a query that shares no content word "
        "with the corpus — the semantic-only case is no longer semantic-only"
    )
    keyword_ids = await keyword_search(query_for(record["query"]))
    assert keyword_ids == [], f"keyword path unexpectedly matched: {keyword_ids}"


# ---------------------------------------------------------------------------
# 5. the merge is real
# ---------------------------------------------------------------------------

async def test_hybrid_merges_both_paths_not_just_one():
    """A query matchable both ways yields a candidate attributed to both paths."""
    record = golden_record("gs-003")
    target = record["expected_memory_ids"][0]

    result = await hybrid_search(query_for(record["query"]))
    by_id = {c.memory_id: c for c in result.candidates}

    assert target in by_id
    merged = by_id[target]
    assert merged.paths == {SEMANTIC, KEYWORD}, (
        f"expected the target to be found by both paths, got {sorted(merged.paths)}"
    )
    assert set(merged.path_scores) == {SEMANTIC, KEYWORD}
    assert set(merged.raw_path_scores) == {SEMANTIC, KEYWORD}
    assert result.path_counts[SEMANTIC] > 0 and result.path_counts[KEYWORD] > 0

    # And the union really is a union: single-path candidates survive the merge.
    assert any(c.paths == {SEMANTIC} for c in result.candidates)

    # Agreement outranks a single path — the zero-filled mean at work.
    assert result.candidates[0].memory_id == target


# ---------------------------------------------------------------------------
# 6. soft-deleted rows are invisible to both paths
# ---------------------------------------------------------------------------

async def test_deleted_memories_excluded_from_both_paths(fresh_subject):
    """`deleted_at IS NULL` is enforced on the semantic AND the keyword query."""
    content = BY_SLUG["lactose"].content  # vector already in the fixture cache
    memory_id = await insert_memory(
        fresh_subject, content, vector=await cached_vector(content)
    )

    # Before deletion both paths must see it, or the post-deletion assertion
    # would pass vacuously.
    before_semantic = await semantic_search(query_for(QUERY_FOODS, fresh_subject))
    before_keyword = await keyword_search(query_for("lactose intolerant", fresh_subject))
    assert [c.memory_id for c in before_semantic] == [memory_id]
    assert [c.memory_id for c in before_keyword] == [memory_id]

    await soft_delete(fresh_subject, memory_id)

    after_semantic = await semantic_search(query_for(QUERY_FOODS, fresh_subject))
    after_keyword = await keyword_search(query_for("lactose intolerant", fresh_subject))
    assert after_semantic == [], f"semantic path returned a soft-deleted row: {after_semantic}"
    assert after_keyword == [], f"keyword path returned a soft-deleted row: {after_keyword}"

    # The row is still physically present — this is a soft delete, not a purge.
    async with session(fresh_subject, fresh_subject) as conn:
        cur = await conn.execute(
            "SELECT deleted_at FROM memories WHERE id = %s::uuid", (memory_id,)
        )
        row = await cur.fetchone()
    assert row is not None and row["deleted_at"] is not None


# ---------------------------------------------------------------------------
# 7. the auth boundary
# ---------------------------------------------------------------------------

async def test_retrieval_scoped_to_subject_id():
    """A query as subject A never returns subject B's rows.

    Both the explicit `subject_id` predicate and the RLS policy are in play
    here; the test asserts the observable behaviour of the pair. It also asserts
    each subject CAN see its own row, so a total failure to retrieve anything
    cannot masquerade as correct isolation.
    """
    subject_a = str(uuid.uuid4())
    subject_b = str(uuid.uuid4())
    content = BY_SLUG["lactose"].content
    vector = await cached_vector(content)

    id_a = await insert_memory(subject_a, content, vector=vector)
    id_b = await insert_memory(subject_b, content, vector=vector)

    result_a = await hybrid_search(query_for(QUERY_FOODS, subject_a))
    ids_a = {c.memory_id for c in result_a.candidates}
    assert id_a in ids_a, "subject A cannot retrieve its own memory"
    assert id_b not in ids_a, "subject A retrieved subject B's memory — scoping is broken"

    result_b = await hybrid_search(query_for(QUERY_FOODS, subject_b))
    ids_b = {c.memory_id for c in result_b.candidates}
    assert id_b in ids_b
    assert id_a not in ids_b

    # The golden corpus is a third subject and must be invisible to both.
    assert not (ids_a | ids_b) & {m.id for m in seed_memories.MEMORIES}

    # And with mismatched GUCs, RLS fails closed rather than open: actor_id is
    # part of every policy predicate, so a session claiming a different actor
    # sees nothing at all.
    mismatched = RetrievalQuery(text=QUERY_FOODS, subject_id=subject_a, actor_id=subject_b)
    assert await semantic_search(mismatched) == []
    assert await keyword_search(
        RetrievalQuery(text="lactose intolerant", subject_id=subject_a, actor_id=subject_b)
    ) == []

    # This test builds its subjects by hand rather than through `fresh_subject`,
    # so it cleans up after itself. Same reasoning as that fixture's docstring.
    for subject in (subject_a, subject_b):
        async with session(subject, subject) as conn:
            await conn.execute(
                "DELETE FROM memories WHERE subject_id = %s::uuid", (subject,)
            )
