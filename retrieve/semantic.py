"""Semantic path (plan step 2): pgvector HNSW cosine search.

Embeds the query through the single LLM seam (`llm.config.embed`) and runs

    ORDER BY embedding <=> $1 LIMIT k

against the subject's non-deleted rows. `<=>` is pgvector's cosine *distance*
operator and is what `memories_embedding_hnsw_idx` (created with
`vector_cosine_ops` in migration 0003) is built for — using any other operator
here silently drops the index and degrades to a sequential scan.

The raw score handed to the merge is cosine *similarity* (`1 - distance`), not
distance, so that "bigger is better" holds for both paths before normalization.

QUERY EMBEDDING, RATE LIMITS, AND THE CACHE SEAM
------------------------------------------------
Every semantic search costs one embedding round-trip, and the provider meters
those. The Voyage account this project runs on is on the no-payment-method tier:
**3 requests per minute**, metered per HTTP request rather than per text. Nine
golden-set queries embedded one at a time is a guaranteed 429; the same nine
sent as one list is a single request.

So this module exposes two entry points:

  `embed_query(text)`     one query, cache-first, with rate-limit backoff.
  `warm_query_cache(...)` many queries, **one** batched request, populating the
                          cache so subsequent `embed_query` calls are free.

`evals/run_eval.py` and the retrieval tests call `warm_query_cache()` once up
front, then run retrieval against a warm cache. That is not a shortcut around
the provider — the vectors are real Voyage embeddings — it is the difference
between 1 request and N, and it makes eval runs reproducible, which plan step 10
asks for explicitly.

Persistence is **opt-in**: set `RETRIEVE_EMBED_CACHE` to a file path and the
cache survives across processes. Unset (the production default) the cache is
process-local, so a long-running API keeps hot queries cheap but never writes
vectors to disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path

from llm.config import embed, resolve_embedding_model
from retrieve import config
from retrieve.types import SEMANTIC, RetrievalCandidate, RetrievalQuery
from store.db import load_env, session

logger = logging.getLogger(__name__)

# Columns every path returns, so a merged candidate has the same metadata
# regardless of which path found it. M4's ranker reads importance /
# reinforcement_count / last_accessed_at straight out of this.
_COLUMNS = """
    m.id, m.content, m.source, m.importance, m.confidence,
    m.weight, m.reinforcement_count, m.created_at, m.last_accessed_at
"""

_SQL = f"""
SELECT {_COLUMNS},
       (m.embedding <=> %(vec)s::vector) AS distance
FROM memories m
WHERE m.subject_id = %(subject_id)s::uuid
  AND m.deleted_at IS NULL
  AND m.embedding IS NOT NULL
ORDER BY m.embedding <=> %(vec)s::vector, m.id
LIMIT %(limit)s
"""


def to_vector_literal(vector: list[float]) -> str:
    """Render a Python float list as a pgvector literal.

    psycopg has no native adapter for `vector` unless the pgvector-python
    extras are registered on every connection; a text literal plus an explicit
    `::vector` cast is driver-agnostic and costs nothing measurable.
    """
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


# ---------------------------------------------------------------------------
# query embedding: cache + batching + rate-limit backoff
# ---------------------------------------------------------------------------

# Process-local, always on. Keyed by model so switching EMBEDDING_MODEL cannot
# serve stale vectors of the wrong width.
_MEMORY_CACHE: dict[str, list[float]] = {}

# How long to wait after a provider rate-limit rejection. The free Voyage tier
# meters over a 60s window at 3 RPM, so anything under ~20s just burns another
# rejection.
RATE_LIMIT_BACKOFF_SECONDS = 21.0
RATE_LIMIT_MAX_ATTEMPTS = 3

_RATE_LIMIT_MARKERS = ("429", "rate limit", "ratelimit", "too many requests")


def _cache_key(model: str, text: str) -> str:
    return f"{model}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _cache_path() -> Path | None:
    load_env()
    raw = os.environ.get("RETRIEVE_EMBED_CACHE")
    return Path(raw) if raw else None


def _load_disk_cache() -> dict[str, list[float]]:
    path = _cache_path()
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_disk_cache(entries: dict[str, list[float]]) -> None:
    path = _cache_path()
    if path is None:
        return
    merged = _load_disk_cache()
    merged.update(entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged), encoding="utf-8")


def _is_rate_limit(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


async def _embed_batch(texts: list[str]) -> list[list[float]]:
    """One batched embedding request, retrying only on provider rate limits.

    Any other failure propagates immediately — a 401 or a malformed request will
    not fix itself in 21 seconds, and retrying it would only delay the error the
    caller needs to see.
    """
    last: Exception | None = None
    for attempt in range(1, RATE_LIMIT_MAX_ATTEMPTS + 1):
        try:
            return await embed(texts)
        except Exception as exc:  # noqa: BLE001 - re-raised below unless throttled
            last = exc
            if not _is_rate_limit(exc) or attempt == RATE_LIMIT_MAX_ATTEMPTS:
                raise
            logger.warning(
                "embedding provider rate-limited (attempt %d/%d); backing off %.0fs",
                attempt,
                RATE_LIMIT_MAX_ATTEMPTS,
                RATE_LIMIT_BACKOFF_SECONDS,
            )
            await asyncio.sleep(RATE_LIMIT_BACKOFF_SECONDS)
    raise last  # pragma: no cover - unreachable, the loop always returns or raises


async def warm_query_cache(texts: list[str]) -> int:
    """Embed every uncached text in **one** request. Returns how many were new.

    This is the rate-limit-safe way to prepare a batch of queries: the provider
    meters requests, not texts, so N queries in one list cost the same as one.
    """
    model = resolve_embedding_model()
    disk = _load_disk_cache()
    if disk:
        _MEMORY_CACHE.update(disk)

    missing: list[str] = []
    for text in texts:
        key = _cache_key(model, text)
        if key not in _MEMORY_CACHE and text not in missing:
            missing.append(text)

    if not missing:
        return 0

    vectors = await _embed_batch(missing)
    fresh = {
        _cache_key(model, text): vector for text, vector in zip(missing, vectors)
    }
    _MEMORY_CACHE.update(fresh)
    _save_disk_cache(fresh)
    return len(missing)


def prune_persistent_cache(keep: list[str]) -> int:
    """Drop persisted query vectors outside `keep`. Returns how many went.

    Only touches the on-disk cache, and only when one is configured — the
    in-process cache is left alone, since dropping a vector a live retriever is
    about to reuse would buy nothing and cost a request.

    This is maintenance for the eval harness, not something a serving path
    should ever call: `run_eval` prunes to the suite's own queries so the file
    stays a faithful record of the suite. Without it the cache only grows, since
    changing a query's text changes its sha256 key and orphans the old vector
    rather than replacing it — which is how it ended up holding retired probes
    like "organ" and "Beethoven" long after those designs were abandoned.
    """
    path = _cache_path()
    if path is None:
        return 0
    cache = _load_disk_cache()
    if not cache:
        return 0
    model = resolve_embedding_model()
    live = {_cache_key(model, text) for text in keep}
    orphans = [key for key in cache if key not in live]
    if orphans:
        for key in orphans:
            del cache[key]
        path.write_text(json.dumps(cache), encoding="utf-8")
    return len(orphans)


async def embed_query(text: str) -> list[float] | None:
    """Vector for one query string, served from cache when possible."""
    model = resolve_embedding_model()
    key = _cache_key(model, text)

    if key in _MEMORY_CACHE:
        return _MEMORY_CACHE[key]

    disk = _load_disk_cache()
    if key in disk:
        _MEMORY_CACHE.update(disk)
        return _MEMORY_CACHE[key]

    vectors = await _embed_batch([text])
    if not vectors or not vectors[0]:
        return None
    _MEMORY_CACHE[key] = vectors[0]
    _save_disk_cache({key: vectors[0]})
    return vectors[0]


# ---------------------------------------------------------------------------
# the path
# ---------------------------------------------------------------------------

def _row_metadata(row: dict) -> dict:
    return {
        "source": row.get("source"),
        "importance": row.get("importance"),
        "confidence": row.get("confidence"),
        "weight": row.get("weight"),
        "reinforcement_count": row.get("reinforcement_count"),
        "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
        "last_accessed_at": (
            row.get("last_accessed_at").isoformat() if row.get("last_accessed_at") else None
        ),
    }


async def semantic_search(query: RetrievalQuery) -> list[RetrievalCandidate]:
    """Return up to `semantic_top_k` candidates tagged `path="semantic"`."""
    if query.is_blank:
        return []

    limit = query.semantic_top_k or config.semantic_top_k()

    vector = await embed_query(query.text)
    if not vector:
        logger.warning("semantic path: embedding provider returned no vector for the query")
        return []
    vec = to_vector_literal(vector)

    async with session(query.subject_id, query.actor_id) as conn:
        cur = await conn.execute(
            _SQL, {"vec": vec, "subject_id": str(query.subject_id), "limit": limit}
        )
        rows = await cur.fetchall()

    candidates: list[RetrievalCandidate] = []
    for row in rows:
        distance = float(row["distance"])
        similarity = 1.0 - distance
        meta = _row_metadata(row)
        meta["cosine_distance"] = distance
        candidates.append(
            RetrievalCandidate(
                memory_id=str(row["id"]),
                content=row["content"],
                score=similarity,
                path=SEMANTIC,
                paths={SEMANTIC},
                raw_path_scores={SEMANTIC: similarity},
                metadata=meta,
            )
        )
    logger.debug("semantic path returned %d candidates", len(candidates))
    return candidates
