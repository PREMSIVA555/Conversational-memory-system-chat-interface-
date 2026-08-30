"""Retrieval interfaces (plan step 1).

`RetrievalQuery`     — what the caller asks for.
`RetrievalCandidate` — one memory a path proposed, with its per-path scores.
`HybridResult`       — the merged candidate list plus which paths degraded.

A candidate produced by a single path carries `paths == {that path}`. After
`retrieve.hybrid` merges by `memory_id`, a memory found by both paths carries
`paths == {"semantic", "keyword"}` and two entries in `path_scores` — that set
is the evidence the merge actually happened rather than one path winning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Path = Literal["semantic", "keyword"]

SEMANTIC: Path = "semantic"
KEYWORD: Path = "keyword"


@dataclass(slots=True)
class RetrievalQuery:
    """One retrieval request, already scoped to an RLS identity.

    `subject_id` / `actor_id` are the M1 schema seam: whose memory, and who is
    reading. They are passed straight into `store.db.session()` and become the
    `app.subject_id` / `app.actor_id` GUCs that every RLS policy reads, so a
    query cannot be run without an identity.
    """

    text: str
    subject_id: str
    actor_id: str
    semantic_top_k: int | None = None
    keyword_top_k: int | None = None

    @property
    def is_blank(self) -> bool:
        """True for '' / whitespace — both paths short-circuit to empty."""
        return not (self.text or "").strip()


@dataclass(slots=True)
class RetrievalCandidate:
    """A memory proposed by one or more retrieval paths.

    score          merged, comparable-across-paths score (set by the merge)
    path           the single best-scoring path that proposed this candidate
    paths          every path that proposed it
    path_scores    normalized 0..1 score per path
    raw_path_scores  the pre-normalization number per path, kept for debugging:
                   cosine similarity for semantic, ts_rank for keyword
    """

    memory_id: str
    content: str
    score: float = 0.0
    path: Path = SEMANTIC
    paths: set[str] = field(default_factory=set)
    path_scores: dict[str, float] = field(default_factory=dict)
    raw_path_scores: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "score": round(self.score, 6),
            "path": self.path,
            "paths": sorted(self.paths),
            "path_scores": {k: round(v, 6) for k, v in self.path_scores.items()},
            "raw_path_scores": {k: round(v, 6) for k, v in self.raw_path_scores.items()},
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class HybridResult:
    """Merged output plus the honest record of what went wrong.

    `degraded` is non-empty when a path timed out or raised. The candidate list
    is still returned in that case — a half-degraded retrieval beats no
    retrieval — but the caller can see it happened.
    """

    candidates: list[RetrievalCandidate] = field(default_factory=list)
    degraded: dict[str, str] = field(default_factory=dict)
    path_counts: dict[str, int] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    @property
    def is_degraded(self) -> bool:
        return bool(self.degraded)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [c.to_dict() for c in self.candidates],
            "degraded": dict(self.degraded),
            "path_counts": dict(self.path_counts),
            "elapsed_ms": round(self.elapsed_ms, 2),
        }
