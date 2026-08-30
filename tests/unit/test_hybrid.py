"""M3 unit tests — the four cases that need no database.

Everything here patches `retrieve.hybrid`'s two path functions, so these tests
exercise the fan-out/isolation/merge logic in isolation from Postgres and from
the embedding provider. That is deliberate: concurrency and error isolation are
properties of `hybrid.py` alone, and proving them against a live DB would let a
fast database hide a sequential await.

Run:  pytest tests/unit/test_hybrid.py -v
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from evals import metrics
from retrieve import hybrid
from retrieve.types import KEYWORD, SEMANTIC, RetrievalCandidate, RetrievalQuery

QUERY = RetrievalQuery(
    text="anything",
    subject_id="11111111-1111-1111-1111-111111111111",
    actor_id="11111111-1111-1111-1111-111111111111",
)


def _candidate(memory_id: str, path: str, raw: float) -> RetrievalCandidate:
    return RetrievalCandidate(
        memory_id=memory_id,
        content=f"content of {memory_id}",
        score=raw,
        path=path,
        paths={path},
        raw_path_scores={path: raw},
    )


# ---------------------------------------------------------------------------
# 1. the two paths genuinely run concurrently
# ---------------------------------------------------------------------------

async def test_paths_run_concurrently(monkeypatch):
    """Total elapsed must sit near max(a, b), not a + b.

    This is the test that a sequential `await semantic; await keyword`
    implementation fails. The two delays are deliberately different so
    max(a, b) and a + b are far apart and the assertion cannot pass by accident.
    """
    semantic_delay = 0.30
    keyword_delay = 0.20

    async def slow_semantic(query):
        await asyncio.sleep(semantic_delay)
        return [_candidate("mem-s", SEMANTIC, 0.9)]

    async def slow_keyword(query):
        await asyncio.sleep(keyword_delay)
        return [_candidate("mem-k", KEYWORD, 0.05)]

    monkeypatch.setattr(hybrid, "semantic_search", slow_semantic)
    monkeypatch.setattr(hybrid, "keyword_search", slow_keyword)

    started = time.perf_counter()
    result = await hybrid.hybrid_search(QUERY)
    elapsed = time.perf_counter() - started

    sequential = semantic_delay + keyword_delay      # 0.50s
    concurrent = max(semantic_delay, keyword_delay)  # 0.30s

    assert len(result.candidates) == 2, result.candidates
    assert abs(elapsed - concurrent) < abs(elapsed - sequential), (
        f"elapsed {elapsed:.3f}s is closer to sequential {sequential:.3f}s than to "
        f"concurrent {concurrent:.3f}s — the paths are not running under asyncio.gather"
    )
    # Hard ceiling as well, so a pathologically slow machine cannot make the
    # relative comparison above pass while the code is still sequential.
    assert elapsed < sequential * 0.9, f"elapsed {elapsed:.3f}s ~ sequential {sequential:.3f}s"


# ---------------------------------------------------------------------------
# 2. one path failing must not sink the other
# ---------------------------------------------------------------------------

async def test_one_path_failure_still_returns_other_path(monkeypatch, caplog):
    """Keyword raises; semantic results must still come back, and it is logged."""

    async def ok_semantic(query):
        return [_candidate("mem-s", SEMANTIC, 0.9)]

    async def boom_keyword(query):
        raise RuntimeError("tsquery exploded")

    monkeypatch.setattr(hybrid, "semantic_search", ok_semantic)
    monkeypatch.setattr(hybrid, "keyword_search", boom_keyword)

    with caplog.at_level(logging.WARNING, logger="retrieve.hybrid"):
        result = await hybrid.hybrid_search(QUERY)

    assert [c.memory_id for c in result.candidates] == ["mem-s"], (
        "the surviving path's results were lost when the other path raised"
    )
    assert result.is_degraded
    assert KEYWORD in result.degraded, result.degraded
    assert "tsquery exploded" in result.degraded[KEYWORD]
    assert SEMANTIC not in result.degraded

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a path failure must be logged as a degradation, not swallowed silently"
    assert any("degraded" in r.getMessage().lower() for r in warnings), [
        r.getMessage() for r in warnings
    ]


async def test_path_timeout_is_isolated_like_a_failure(monkeypatch):
    """A hung path must time out and degrade, not hang the whole retrieval."""

    async def ok_semantic(query):
        return [_candidate("mem-s", SEMANTIC, 0.9)]

    async def hanging_keyword(query):
        await asyncio.sleep(30)
        raise AssertionError("should have been cancelled by the per-path timeout")

    monkeypatch.setattr(hybrid, "semantic_search", ok_semantic)
    monkeypatch.setattr(hybrid, "keyword_search", hanging_keyword)
    monkeypatch.setenv("RETRIEVE_PATH_TIMEOUT_MS", "150")

    started = time.perf_counter()
    result = await hybrid.hybrid_search(QUERY)
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, f"per-path timeout did not fire: {elapsed:.2f}s"
    assert [c.memory_id for c in result.candidates] == ["mem-s"]
    assert "timeout" in result.degraded.get(KEYWORD, ""), result.degraded


# ---------------------------------------------------------------------------
# 3. empty input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
async def test_empty_query_returns_empty_not_error(monkeypatch, text):
    """A blank query yields [] and never reaches a path (so never reaches the DB)."""
    called: list[str] = []

    async def tripwire_semantic(query):
        called.append(SEMANTIC)
        return []

    async def tripwire_keyword(query):
        called.append(KEYWORD)
        return []

    monkeypatch.setattr(hybrid, "semantic_search", tripwire_semantic)
    monkeypatch.setattr(hybrid, "keyword_search", tripwire_keyword)

    query = RetrievalQuery(text=text, subject_id=QUERY.subject_id, actor_id=QUERY.actor_id)
    result = await hybrid.hybrid_search(query)

    assert result.candidates == []
    assert result.degraded == {}
    assert called == [], (
        "a blank query short-circuited too late — it still hit a retrieval path"
    )
    assert await hybrid.retrieve(query) == []


# ---------------------------------------------------------------------------
# 4. the merge and the normalization
# ---------------------------------------------------------------------------

async def test_merge_unions_paths_and_keeps_both_scores(monkeypatch):
    """A memory found by both paths carries both scores and both path tags."""

    async def sem(query):
        return [_candidate("shared", SEMANTIC, 0.9), _candidate("sem-only", SEMANTIC, 0.5)]

    async def kw(query):
        return [_candidate("shared", KEYWORD, 0.08), _candidate("kw-only", KEYWORD, 0.01)]

    monkeypatch.setattr(hybrid, "semantic_search", sem)
    monkeypatch.setattr(hybrid, "keyword_search", kw)

    result = await hybrid.hybrid_search(QUERY)
    by_id = {c.memory_id: c for c in result.candidates}

    assert set(by_id) == {"shared", "sem-only", "kw-only"}
    assert by_id["shared"].paths == {SEMANTIC, KEYWORD}
    assert set(by_id["shared"].path_scores) == {SEMANTIC, KEYWORD}
    assert by_id["sem-only"].paths == {SEMANTIC}
    assert by_id["kw-only"].paths == {KEYWORD}

    # Zero-filled mean: the doc both paths agreed on must outrank both singles.
    assert by_id["shared"].score > by_id["sem-only"].score
    assert by_id["shared"].score > by_id["kw-only"].score
    assert result.candidates[0].memory_id == "shared"


def test_normalization_uses_absolute_scales_not_min_max():
    """Scores depend only on the raw value, never on what else the path returned.

    This is the regression test for the min-max tie: under the old
    normalization, whichever candidate happened to be best in a result set was
    mapped to 1.0 however weak it actually was, so a lone `ts_rank` 0.06 keyword
    hit scored the same as an excellent semantic match.
    """
    from retrieve import config

    # --- semantic: the cosine similarity itself, clamped ------------------
    cands = [
        _candidate("a", SEMANTIC, 0.9),
        _candidate("b", SEMANTIC, 0.5),
        _candidate("c", SEMANTIC, 0.3),
    ]
    hybrid._normalize(cands, SEMANTIC)
    scores = [c.path_scores[SEMANTIC] for c in cands]
    assert scores == [pytest.approx(0.9), pytest.approx(0.5), pytest.approx(0.3)]

    # A uniformly weak result set must stay weak — nothing is promoted to 1.0
    # just for being the best of a bad bunch.
    weak = [_candidate("w1", SEMANTIC, 0.30), _candidate("w2", SEMANTIC, 0.25)]
    hybrid._normalize(weak, SEMANTIC)
    assert weak[0].path_scores[SEMANTIC] == pytest.approx(0.30)
    assert max(c.path_scores[SEMANTIC] for c in weak) < 0.5

    # Out-of-range similarities are clamped, not wrapped.
    clamped = [_candidate("neg", SEMANTIC, -0.4)]
    hybrid._normalize(clamped, SEMANTIC)
    assert clamped[0].path_scores[SEMANTIC] == 0.0

    # --- keyword: saturating against a fixed reference --------------------
    ref = config.TS_RANK_REFERENCE
    single = [_candidate("solo", KEYWORD, ref)]
    hybrid._normalize(single, KEYWORD)
    assert single[0].path_scores[KEYWORD] == pytest.approx(0.5), (
        "a canonical single-term match must land mid-band, not at the maximum"
    )

    richer = [_candidate("rich", KEYWORD, ref * 3)]
    hybrid._normalize(richer, KEYWORD)
    assert 0.5 < richer[0].path_scores[KEYWORD] < 1.0

    # Identical raw scores normalize identically regardless of set membership —
    # that is what "absolute" means.
    alone = [_candidate("x", KEYWORD, 0.02)]
    crowded = [_candidate("x", KEYWORD, 0.02), _candidate("y", KEYWORD, 0.9)]
    hybrid._normalize(alone, KEYWORD)
    hybrid._normalize(crowded, KEYWORD)
    assert alone[0].path_scores[KEYWORD] == pytest.approx(crowded[0].path_scores[KEYWORD])

    # Monotonic, so within-path ordering is untouched.
    assert crowded[1].path_scores[KEYWORD] > crowded[0].path_scores[KEYWORD]


async def test_lone_keyword_hit_outranks_a_weak_semantic_field(monkeypatch):
    """The measured tie that motivated absolute scales must not come back.

    This is gs-002's exact shape: five weak semantic neighbours plus one exact
    lexical match. Under min-max both paths' best result normalized to 1.0, both
    merged to 0.500000, and the winner was decided by UUID sort order. The
    lexical match must now win outright.
    """

    async def weak_semantic(query):
        return [_candidate(f"weak{i}", SEMANTIC, 0.30 - i * 0.01) for i in range(5)]

    async def one_exact(query):
        return [_candidate("target", KEYWORD, 0.0607927)]

    monkeypatch.setattr(hybrid, "semantic_search", weak_semantic)
    monkeypatch.setattr(hybrid, "keyword_search", one_exact)

    result = await hybrid.hybrid_search(QUERY)

    assert result.candidates[0].memory_id == "target", (
        "a weak semantic field beat an exact keyword match: "
        f"{[(c.memory_id, round(c.score, 4)) for c in result.candidates]}"
    )
    scores = {c.memory_id: round(c.score, 6) for c in result.candidates}
    assert len(set(scores.values())) == len(scores), (
        f"scores tied, so ordering fell back to id sort: {scores}"
    )


# ---------------------------------------------------------------------------
# 5. metrics arithmetic, checked against hand-computed values
# ---------------------------------------------------------------------------

def test_metrics_math_is_correct():
    """Hand-computed cases, including every degenerate denominator."""
    # retrieved {a,b,c,d}, expected {a,b,e}. hits = {a,b} = 2.
    #   precision = 2/4 = 0.5 ; recall = 2/3 ; f1 = 2*.5*(2/3)/(.5+2/3) = 0.571428...
    retrieved = ["a", "b", "c", "d"]
    expected = ["a", "b", "e"]
    assert metrics.precision(retrieved, expected) == pytest.approx(0.5)
    assert metrics.recall(retrieved, expected) == pytest.approx(2 / 3)
    assert metrics.f1(0.5, 2 / 3) == pytest.approx(0.5714285714, abs=1e-9)

    # perfect
    assert metrics.precision(["a"], ["a"]) == 1.0
    assert metrics.recall(["a"], ["a"]) == 1.0

    # disjoint
    assert metrics.precision(["x"], ["a"]) == 0.0
    assert metrics.recall(["x"], ["a"]) == 0.0
    assert metrics.f1(0.0, 0.0) == 0.0

    # degenerate denominators, per the documented convention
    assert metrics.precision([], ["a"]) == 0.0
    assert metrics.recall(["a"], []) == 1.0
    assert metrics.precision([], []) == 0.0
    assert metrics.recall([], []) == 1.0

    # duplicates in `retrieved` must not inflate the denominator (set semantics)
    assert metrics.precision(["a", "a"], ["a"]) == 1.0

    # score_query truncates to k before scoring: only 'a' survives k=1.
    score = metrics.score_query("q1", ["a", "b", "c", "d"], ["a", "b", "e"], k=1)
    assert score.retrieved == ["a"]
    assert score.precision == pytest.approx(1.0)
    assert score.recall == pytest.approx(1 / 3)
    assert score.hits == ["a"]

    # --- EVERY metric shares the same cutoff -----------------------------
    # A hit that falls outside k must read as a miss on all five, not as
    # "recall says missed, MRR says found". This is the convention M8 relies on
    # when it reads these keys side by side.
    beyond = metrics.score_query("q2", ["x", "y", "z", "w", "target"], ["target"], k=3)
    assert beyond.recall == 0.0
    assert beyond.precision == 0.0
    assert beyond.reciprocal_rank == 0.0, (
        "MRR counted a hit beyond the cutoff that recall counted as a miss — "
        "the two metric families disagree about what 'found' means"
    )
    assert beyond.precision_at_r == 0.0

    # Inside the cutoff, rank position is still reflected faithfully.
    inside = metrics.score_query("q3", ["x", "target", "z"], ["target"], k=3)
    assert inside.recall == 1.0
    assert inside.reciprocal_rank == pytest.approx(0.5)   # second position
    assert inside.precision_at_r == 0.0                   # R=1, top-1 is 'x'

    first = metrics.score_query("q4", ["target", "x"], ["target"], k=3)
    assert first.reciprocal_rank == 1.0
    assert first.precision_at_r == 1.0

    # aggregate: macro is the mean of per-query metrics.
    #   q1: p=1.0,   r=1.0
    #   q2: p=0.0,   r=0.0
    #   macro p = 0.5, macro r = 0.5
    #   micro: hits 1, retrieved 2, expected 2 -> p=0.5, r=0.5
    agg = metrics.aggregate(
        [
            metrics.score_query("q1", ["a"], ["a"]),
            metrics.score_query("q2", ["z"], ["b"]),
        ]
    )
    assert agg["queries"] == 2
    assert agg["macro_precision"] == pytest.approx(0.5)
    assert agg["macro_recall"] == pytest.approx(0.5)
    assert agg["micro_precision"] == pytest.approx(0.5)
    assert agg["micro_recall"] == pytest.approx(0.5)
    assert agg["precision"] == agg["macro_precision"]
    assert agg["recall"] == agg["macro_recall"]

    assert metrics.aggregate([])["queries"] == 0
