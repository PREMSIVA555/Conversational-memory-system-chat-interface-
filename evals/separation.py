"""Whole-corpus path-separation measurement.

WHY THIS MODULE EXISTS
----------------------
The golden set's keyword-only and semantic-only probes are only worth anything
if the *other* path genuinely misses the target — and "misses" has to mean
"misses by a margin", not "happens to fall on the far side of the cutoff".

The first version of gs-002 taught this the hard way. It asserted membership at
the configured k: `target not in semantic_top_k`. That assertion passed while
the target sat 0.00128 cosine outside the boundary — smaller than voyage-3.5's
own ~1e-3 run-to-run drift — so the probe was a coin flip that happened to be
landing heads. A membership assertion cannot tell the difference between a
0.0013 margin and a 0.13 one, which means it cannot detect the erosion of the
very property the probe exists to test.

So this module measures the property directly: rank the ENTIRE corpus by cosine
with no LIMIT, find where the target actually sits, and report its distance from
the top-k boundary. `run_eval` prints it on every run and the integration test
asserts a floor on it, so an eroding margin shows up as a visible number and
then as a failing test, rather than silently reverting to a coin flip.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from retrieve.semantic import embed_query, to_vector_literal
from store.db import session

# The floors the golden set's separation must clear.
#
# MIN_RANK is expressed against the corpus size rather than as a raw position so
# it keeps meaning if the corpus grows. MIN_MARGIN is ~50x voyage-3.5's measured
# ~1e-3 elementwise drift, which is the scale of noise that broke the previous
# design. Measured actuals for gs-002 on the 44-row corpus are rank 31 and
# margin +0.130 at k=5 / +0.103 at k=10, so both floors have ample headroom;
# they are tripwires for erosion, not targets to tune against.
MIN_SEPARATION_RANK = 20
MIN_SEPARATION_MARGIN = 0.05


@dataclass(slots=True)
class SeparationReport:
    """Where a target sits in the full cosine ranking, and by how much."""

    query: str
    corpus_size: int
    target_id: str | None
    target_rank: int | None          # 1-based, over the WHOLE corpus
    target_distance: float | None
    boundary_rank: int               # the k whose boundary `margin` is measured against
    boundary_distance: float | None
    margin: float | None             # target_distance - boundary_distance

    @property
    def is_outside(self) -> bool:
        """True when the target falls outside the top-k cut."""
        return self.target_rank is not None and self.target_rank > self.boundary_rank

    def summary(self) -> str:
        if self.target_rank is None:
            return f"target not present in corpus (n={self.corpus_size})"
        return (
            f"semantic rank {self.target_rank}/{self.corpus_size}, "
            f"margin@{self.boundary_rank} {self.margin:+.5f} "
            f"({'outside' if self.is_outside else 'INSIDE'})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_size": self.corpus_size,
            "target_id": self.target_id,
            "target_rank": self.target_rank,
            "target_distance": (
                round(self.target_distance, 6) if self.target_distance is not None else None
            ),
            "boundary_rank": self.boundary_rank,
            "boundary_distance": (
                round(self.boundary_distance, 6) if self.boundary_distance is not None else None
            ),
            "margin": round(self.margin, 6) if self.margin is not None else None,
            "outside_top_k": self.is_outside,
        }


_SQL = """
SELECT m.id, (m.embedding <=> %(vec)s::vector) AS distance
FROM memories m
WHERE m.subject_id = %(subject_id)s::uuid
  AND m.deleted_at IS NULL
  AND m.embedding IS NOT NULL
ORDER BY m.embedding <=> %(vec)s::vector, m.id
"""


async def measure_separation(
    query: str,
    expected_ids: Iterable[str],
    *,
    subject_id: str,
    actor_id: str,
    boundary_rank: int,
) -> SeparationReport:
    """Rank the whole corpus for `query`; report where the best target lands.

    Deliberately runs WITHOUT a LIMIT. A LIMIT is exactly what hides the number
    we care about: it can only tell you whether the target made the cut, never
    how close the call was.

    `margin` is `target_distance - boundary_distance`, so it is POSITIVE when
    the target is farther away than the row at `boundary_rank` — i.e. genuinely
    outside the cut — and its size is how much room the probe has before noise
    or a corpus edit flips it.

    Uses the same cached query embedding as the retriever, so the number
    reported is the one retrieval actually acted on.
    """
    targets = {str(e) for e in expected_ids}
    vector = await embed_query(query)
    if not vector:
        raise RuntimeError(f"no embedding returned for query {query!r}")

    async with session(subject_id, actor_id) as conn:
        cur = await conn.execute(
            _SQL, {"vec": to_vector_literal(vector), "subject_id": str(subject_id)}
        )
        rows = await cur.fetchall()

    ranked: Sequence[tuple[str, float]] = [
        (str(r["id"]), float(r["distance"])) for r in rows
    ]
    corpus_size = len(ranked)
    boundary_distance = (
        ranked[boundary_rank - 1][1] if 0 < boundary_rank <= corpus_size else None
    )

    for position, (memory_id, distance) in enumerate(ranked, start=1):
        if memory_id in targets:
            return SeparationReport(
                query=query,
                corpus_size=corpus_size,
                target_id=memory_id,
                target_rank=position,
                target_distance=distance,
                boundary_rank=boundary_rank,
                boundary_distance=boundary_distance,
                margin=(
                    distance - boundary_distance if boundary_distance is not None else None
                ),
            )

    return SeparationReport(
        query=query,
        corpus_size=corpus_size,
        target_id=None,
        target_rank=None,
        target_distance=None,
        boundary_rank=boundary_rank,
        boundary_distance=boundary_distance,
        margin=None,
    )
