"""Read path: hybrid retrieval over the `memories` table.

Two paths, run concurrently:

    semantic  pgvector HNSW cosine search on `memories.embedding`
    keyword   Postgres tsvector/GIN search on `memories.content_tsv`

Graph search is deliberately absent — see retrieve/README.md.
"""

from retrieve.types import RetrievalCandidate, RetrievalQuery, HybridResult

__all__ = ["RetrievalCandidate", "RetrievalQuery", "HybridResult"]
