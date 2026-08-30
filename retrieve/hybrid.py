"""Hybrid retrieval (plan steps 4, 5, 6): fan out, isolate, normalize, merge.

    query ─┬─> semantic_search ─┐
           └─> keyword_search  ─┴─> normalize per path ─> merge by memory_id

Three properties this module exists to guarantee:

1. **Concurrency (step 4).** Both paths are launched into one `asyncio.gather`.
   The semantic path pays for an embedding round-trip and the keyword path pays
   for a GIN scan; running them in series would add those latencies. Total wall
   time is max(a, b), not a + b — `test_paths_run_concurrently` measures it.

2. **Per-path isolation (step 5).** Each path runs inside its own
   `asyncio.wait_for` and its own `except`. A path that raises or times out
   contributes an empty list and an entry in `HybridResult.degraded`; the other
   path's results are still returned and the degradation is logged at WARNING.
   Nothing here re-raises. A half-degraded retrieval is worth more to the caller
   than an exception, and M5's circuit breaker is the layer that decides when
   degradation has gone on too long.

3. **Comparable scores (step 6).** See `_normalize` — cosine similarity and
   `ts_rank` are not on the same scale and must not be compared raw.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from retrieve import config
from retrieve.keyword import keyword_search
from retrieve.semantic import semantic_search
from retrieve.types import KEYWORD, SEMANTIC, HybridResult, RetrievalCandidate, RetrievalQuery

logger = logging.getLogger(__name__)

PathFn = Callable[[RetrievalQuery], Awaitable[list[RetrievalCandidate]]]


# ---------------------------------------------------------------------------
# step 6 — score normalization
# ---------------------------------------------------------------------------

def _normalize(candidates: list[RetrievalCandidate], path: str) -> None:
    """Put a path's raw scores on an absolute 0..1 scale, in place.

    WHY NORMALIZE AT ALL
    --------------------
    The two paths produce numbers on incomparable scales:

      semantic  cosine similarity, `1 - (embedding <=> query)`. Bounded in
                [-1, 1] and, for voyage-3.5 over natural sentences, roughly
                0.25 for unrelated text and 0.6-0.85 for a real match.
      keyword   `ts_rank`, which is a function of term frequency and document
                length, not of similarity. A single-term match on a short
                document lands near 0.06; more matched terms push it up, and it
                asymptotes below 1.

    Summed raw, the semantic path would dominate every merge by an order of
    magnitude and the keyword path would be decorative.

    WHY NOT MIN-MAX
    ---------------
    Min-max within each result set is the obvious choice and it is WRONG here,
    for a reason worth spelling out because the first implementation of this
    function used it.

    Min-max is corpus-relative: it maps whichever result happens to be best in
    a given result set to 1.0, no matter how bad that result actually is. So a
    query with no good semantic match at all - every candidate a weak 0.29
    similarity - still produced a 1.0, indistinguishable from a query whose top
    hit was a genuine 0.85. It threw away exactly the information the merge
    needs: how good this path's results are *in absolute terms*.

    That had a concrete, measured consequence. For the keyword-only probe
    (gs-002, query "origin") the semantic path returns five weak neighbours and
    the keyword path returns one exact lexical match. Under min-max both the
    best semantic result and the lone keyword result normalized to 1.0, so both
    merged to exactly 0.500000 and the tie was settled by UUID sort order - a
    `ts_rank` of 0.06 scoring identically to the best semantic match, with the
    winner decided by a random identifier. It also matters downstream: M4
    weights `semantic_score` at 0.4, so an inflated path score distorts the
    weighted ranking.

    WHAT THIS DOES INSTEAD
    ----------------------
    Each path maps its raw score through a FIXED reference scale that does not
    depend on what else came back:

      semantic  the cosine similarity itself, clamped to [0, 1]. It is already
                absolute, bounded and corpus-independent - there is nothing to
                gain by rescaling it and a great deal to lose.

      keyword   a saturating transform against a fixed reference,
                `rank / (rank + TS_RANK_REFERENCE)`. TS_RANK_REFERENCE is the
                `ts_rank` of a single-term match on a short document, so that
                canonical case scores 0.5 - a deliberately mid-band value. A
                richer match (more query terms present) scores above it and
                approaches 1.0; a weaker one falls below. A path that returns
                exactly one result therefore gets a score reflecting how good
                that result is, never an automatic 1.0.

    Both transforms are strictly monotonic, so within-path ordering is exactly
    preserved; only the cross-path comparison changes.
    """
    if not candidates:
        return

    for candidate in candidates:
        raw = candidate.raw_path_scores[path]
        if path == SEMANTIC:
            normalized = max(0.0, min(1.0, raw))
        else:
            reference = config.TS_RANK_REFERENCE
            normalized = raw / (raw + reference) if raw > 0 else 0.0
        candidate.path_scores[path] = normalized
        candidate.score = normalized


# ---------------------------------------------------------------------------
# step 5 — per-path timeout + error isolation
# ---------------------------------------------------------------------------

async def _run_path(
    name: str,
    factory: Callable[[], Awaitable[list[RetrievalCandidate]]],
    timeout_s: float,
    degraded: dict[str, str],
) -> list[RetrievalCandidate]:
    """Await one path, converting any failure into an empty list + a log line.

    The coroutine is built by `factory` *inside* the try, so a path function
    that raises on call (rather than on await) is caught here too.
    """
    started = time.perf_counter()
    try:
        return await asyncio.wait_for(factory(), timeout=timeout_s)
    except asyncio.TimeoutError:
        degraded[name] = f"timeout after {timeout_s * 1000:.0f}ms"
        logger.warning(
            "retrieval degraded: %s path timed out after %.0fms; "
            "continuing with the remaining path(s)",
            name,
            timeout_s * 1000,
        )
        return []
    except Exception as exc:  # noqa: BLE001 - isolation is the entire point
        degraded[name] = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "retrieval degraded: %s path failed after %.0fms (%s: %s); "
            "continuing with the remaining path(s)",
            name,
            (time.perf_counter() - started) * 1000,
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        return []


# ---------------------------------------------------------------------------
# step 4 — the merge
# ---------------------------------------------------------------------------

def _merge(
    per_path: dict[str, list[RetrievalCandidate]],
) -> list[RetrievalCandidate]:
    """Union the paths by `memory_id`, keeping every per-path score.

    Merged score is the zero-filled mean over the paths that *contributed*
    results:

        score = sum(normalized[p] for p in paths, 0 where absent) / n_contributing

    Zero-filling is what makes the merge reward agreement: a memory both paths
    surfaced accumulates two terms and outranks one that only a single path saw,
    without any hand-tuned bonus constant. Dividing by the number of
    *contributing* paths (not the number attempted) keeps scores on the same
    scale whether or not a path returned empty or degraded.
    """
    contributing = [p for p, cands in per_path.items() if cands]
    divisor = max(1, len(contributing))

    merged: dict[str, RetrievalCandidate] = {}
    for path, candidates in per_path.items():
        for candidate in candidates:
            existing = merged.get(candidate.memory_id)
            if existing is None:
                merged[candidate.memory_id] = candidate
                continue
            existing.paths |= candidate.paths
            existing.path_scores.update(candidate.path_scores)
            existing.raw_path_scores.update(candidate.raw_path_scores)
            # Keep whichever path's metadata is richer; both carry the same row.
            existing.metadata.update(candidate.metadata)

    for candidate in merged.values():
        total = sum(candidate.path_scores.get(p, 0.0) for p in per_path)
        candidate.score = total / divisor
        # `path` names the single strongest contributing path; `paths` is the
        # complete set and is what proves a merge happened.
        candidate.path = max(candidate.path_scores, key=candidate.path_scores.get)

    return sorted(
        merged.values(),
        # Deterministic: score desc, then memory_id asc so ties never reorder
        # between runs (the eval harness depends on this).
        key=lambda c: (-c.score, c.memory_id),
    )


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

async def hybrid_search(query: RetrievalQuery) -> HybridResult:
    """Run both paths concurrently and return the merged, scored candidates."""
    if query.is_blank:
        # Short-circuits before any DB or embedding call: a blank query is a
        # no-op, not an error (`test_empty_query_returns_empty_not_error`).
        return HybridResult(
            candidates=[], degraded={}, path_counts={SEMANTIC: 0, KEYWORD: 0}, elapsed_ms=0.0
        )

    timeout_s = config.path_timeout_seconds()
    degraded: dict[str, str] = {}
    started = time.perf_counter()

    # Module-global lookup on purpose: tests monkeypatch
    # `retrieve.hybrid.semantic_search` / `.keyword_search`, and late binding is
    # what makes that work.
    semantic_fn = semantic_search
    keyword_fn = keyword_search

    semantic_cands, keyword_cands = await asyncio.gather(
        _run_path(SEMANTIC, lambda: semantic_fn(query), timeout_s, degraded),
        _run_path(KEYWORD, lambda: keyword_fn(query), timeout_s, degraded),
    )
    elapsed_ms = (time.perf_counter() - started) * 1000

    semantic_cands = list(semantic_cands or [])
    keyword_cands = list(keyword_cands or [])

    _normalize(semantic_cands, SEMANTIC)
    _normalize(keyword_cands, KEYWORD)

    merged = _merge({SEMANTIC: semantic_cands, KEYWORD: keyword_cands})

    return HybridResult(
        candidates=merged,
        degraded=degraded,
        path_counts={SEMANTIC: len(semantic_cands), KEYWORD: len(keyword_cands)},
        elapsed_ms=elapsed_ms,
    )


async def retrieve(query: RetrievalQuery) -> list[RetrievalCandidate]:
    """Convenience wrapper for callers that do not care about degradation."""
    return (await hybrid_search(query)).candidates
