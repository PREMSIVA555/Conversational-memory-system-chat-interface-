"""Keyword path (plan step 3): tsvector/GIN search with `ts_rank`.

`websearch_to_tsquery` is used rather than `plainto_tsquery` because it accepts
the syntax a person actually types — quoted phrases, `or`, leading `-` for
negation — and never raises on malformed input, which `to_tsquery` does.

Note its default semantics: bare terms are **AND**-ed. `websearch_to_tsquery(
'english', 'foods upset stomach')` is `'food' & 'upset' & 'stomach'`, so a
memory must contain every term to match. That is exactly why the golden set's
semantic-only query returns nothing here, and it is a property of the operator,
not an accident of the corpus.

`content_tsv` is a STORED generated column (migration 0002) with a GIN index
(migration 0003), so `content_tsv @@ query` is an index scan.
"""

from __future__ import annotations

import logging

from retrieve import config
from retrieve.types import KEYWORD, RetrievalCandidate, RetrievalQuery
from store.db import session

logger = logging.getLogger(__name__)

_COLUMNS = """
    m.id, m.content, m.source, m.importance, m.confidence,
    m.weight, m.reinforcement_count, m.created_at, m.last_accessed_at
"""

_SQL = f"""
SELECT {_COLUMNS},
       ts_rank(m.content_tsv, q.query) AS rank
FROM websearch_to_tsquery('english', %(text)s) AS q(query),
     memories m
WHERE m.content_tsv @@ q.query
  AND m.subject_id = %(subject_id)s::uuid
  AND m.deleted_at IS NULL
ORDER BY rank DESC, m.id
LIMIT %(limit)s
"""


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


async def keyword_search(query: RetrievalQuery) -> list[RetrievalCandidate]:
    """Return up to `keyword_top_k` candidates tagged `path="keyword"`."""
    if query.is_blank:
        return []

    limit = query.keyword_top_k or config.keyword_top_k()

    async with session(query.subject_id, query.actor_id) as conn:
        cur = await conn.execute(
            _SQL,
            {"text": query.text, "subject_id": str(query.subject_id), "limit": limit},
        )
        rows = await cur.fetchall()

    candidates: list[RetrievalCandidate] = []
    for row in rows:
        rank = float(row["rank"])
        meta = _row_metadata(row)
        meta["ts_rank"] = rank
        candidates.append(
            RetrievalCandidate(
                memory_id=str(row["id"]),
                content=row["content"],
                score=rank,
                path=KEYWORD,
                paths={KEYWORD},
                raw_path_scores={KEYWORD: rank},
                metadata=meta,
            )
        )
    logger.debug("keyword path returned %d candidates", len(candidates))
    return candidates
