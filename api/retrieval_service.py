"""Retrieval, wired into the API layer as an internal function (plan step 15).

M3's job is to make retrieval *callable from the API*, not to put it on the chat
critical path — that lands in M5, behind the circuit breaker and the timeout in
`retrieve/guarded.py`. So this module deliberately stops at the seam:

  * `retrieve_memories()` is the single API-facing entry point. Everything the
    API layer needs from the read path goes through it, so when M5 adds the
    breaker there is exactly one call site to wrap.
  * `router` exposes it over HTTP for manual inspection, and is **not mounted**
    by `api/main.py`. Nothing serves it until someone calls
    `app.include_router(retrieval_service.router)`. That is the whole point of
    "not yet the chat critical path": the capability exists and is exercised by
    tests, but no request path depends on it yet.

Why a plain dict and not `RetrievalCandidate` in the return type: the API
boundary should not leak the retrieval layer's dataclasses to HTTP clients or to
M6's TypeScript. `RetrievalCandidate.to_dict()` is the stable shape.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query

from retrieve.hybrid import hybrid_search
from retrieve.types import RetrievalQuery

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/retrieval", tags=["retrieval (internal)"])


async def retrieve_memories(
    *,
    subject_id: str,
    actor_id: str | None = None,
    query: str,
    limit: int | None = None,
    semantic_top_k: int | None = None,
    keyword_top_k: int | None = None,
) -> dict[str, Any]:
    """Run hybrid retrieval for one subject and return a JSON-safe payload.

    `actor_id` defaults to `subject_id`: in the single-user assistant the M1
    schema seam's two columns always hold the same value, and defaulting here
    means callers cannot accidentally omit the GUC that RLS needs.

    Never raises for a degraded path — `hybrid_search` isolates per-path
    failures and reports them in `degraded`, which is surfaced in the payload so
    a caller (and M5's breaker) can see the read path was only partly healthy.
    """
    result = await hybrid_search(
        RetrievalQuery(
            text=query,
            subject_id=subject_id,
            actor_id=actor_id or subject_id,
            semantic_top_k=semantic_top_k,
            keyword_top_k=keyword_top_k,
        )
    )

    candidates = result.candidates[:limit] if limit else result.candidates
    if result.degraded:
        logger.warning(
            "retrieval for subject %s completed degraded: %s", subject_id, result.degraded
        )

    return {
        "query": query,
        "subject_id": subject_id,
        "count": len(candidates),
        "candidates": [c.to_dict() for c in candidates],
        "path_counts": result.path_counts,
        "degraded": result.degraded,
        "elapsed_ms": round(result.elapsed_ms, 2),
    }


@router.get("")
async def retrieval_probe(
    subject_id: str = Query(..., description="whose memories to search"),
    q: str = Query(..., description="the query text"),
    limit: int = Query(10, ge=1, le=50),
) -> dict[str, Any]:
    """Manual inspection endpoint. Not mounted in `api/main.py` — see module docstring."""
    return await retrieve_memories(subject_id=subject_id, query=q, limit=limit)
