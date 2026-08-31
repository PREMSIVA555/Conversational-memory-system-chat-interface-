"""The ranking node (plan steps 3, 4, 14).

    hybrid candidates ─> score_candidate() ─> sort desc ─> top-k ─> RankedCandidate[]


THE WEIGHTED FORMULA
--------------------
This is the whole of M4's ranking, and it is deliberately four lines long:

    score = 0.4 * semantic_score
          + 0.2 * recency_score
          + 0.2 * frequency_score
          + 0.2 * importance_score

The four weights are `WEIGHT_SEMANTIC = 0.4`, `WEIGHT_RECENCY = 0.2`,
`WEIGHT_FREQUENCY = 0.2`, `WEIGHT_IMPORTANCE = 0.2`. They are **defined once**,
in `retrieve/config.py`, which raises at import time if they do not sum to 1.0;
this module imports them and re-exports them under the same names so a reader
lands on the numbers here and the guarantee there. No literal weight appears
anywhere else in the codebase.

Because every feature in `retrieve/features.py` is bounded to [0, 1] and the
weights sum to 1.0, `score_candidate()` returns a value in [0, 1] for every
possible input. That is a property, not a hope — it is what makes the score
comparable across queries and safe to threshold on later.

DETERMINISM
-----------
`rank()` sorts on `(-score, memory_id)`. The `memory_id` term is not decoration:
scores tie constantly in practice — two memories with identical hand-set signals,
or two near-duplicate facts the M2 extractor produced from one turn ("The user
plays the cello." and "The user plays the cello on Sunday mornings at the
community hall." are two separate rows, below the 0.82 dedup threshold, and will
frequently score within floating-point noise of each other). Without an explicit
tiebreaker, `sorted` would preserve whatever order the merge dict happened to
yield, and the same query would compose a different prompt on different runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from retrieve import config
from retrieve.features import (
    frequency_score,
    importance_score,
    recency_score,
    semantic_score,
    utc_now,
)
from retrieve.types import RetrievalCandidate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# the weights (plan step 3) — re-exported from their single definition
# ---------------------------------------------------------------------------
#
#     semantic    0.4
#     recency     0.2
#     frequency   0.2
#     importance  0.2
#
# Defined in `retrieve/config.py`; imported, never re-spelled, so there is
# exactly one number to change and one import-time check that they sum to 1.0.

WEIGHT_SEMANTIC = config.WEIGHT_SEMANTIC
WEIGHT_RECENCY = config.WEIGHT_RECENCY
WEIGHT_FREQUENCY = config.WEIGHT_FREQUENCY
WEIGHT_IMPORTANCE = config.WEIGHT_IMPORTANCE

RANKING_WEIGHTS = config.RANKING_WEIGHTS

# The plan's weights, written out as executable code so that reading THIS file
# answers "what are the weights?" without a second lookup.
#
# This is a CONFORMANCE CHECK, not a second definition — nothing below reads
# `_PLAN_WEIGHTS`, and `score_breakdown()` uses only the imported constants
# above. Its job is to fail loudly at import if the single definition in
# `retrieve/config.py` ever drifts from what M4 specifies, and to make the
# numbers visible to a human reading the ranking node itself.
_PLAN_WEIGHTS = {
    "semantic": 0.4,
    "recency": 0.2,
    "frequency": 0.2,
    "importance": 0.2,
}

if RANKING_WEIGHTS != _PLAN_WEIGHTS:
    raise RuntimeError(
        "ranking weights have drifted from the M4 specification: "
        f"retrieve/config.py declares {RANKING_WEIGHTS!r}, "
        f"the plan specifies {_PLAN_WEIGHTS!r}"
    )

__all__ = [
    "WEIGHT_SEMANTIC",
    "WEIGHT_RECENCY",
    "WEIGHT_FREQUENCY",
    "WEIGHT_IMPORTANCE",
    "RANKING_WEIGHTS",
    "ScoreBreakdown",
    "RankedCandidate",
    "score_candidate",
    "score_breakdown",
    "rank",
]


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """Per-signal detail behind one score — the debug view (plan step 14).

    Kept alongside the score rather than recomputed on demand, so what gets
    logged is provably the same arithmetic that produced the ranking rather than
    a second evaluation that might disagree.
    """

    semantic: float
    recency: float
    frequency: float
    importance: float
    total: float

    def to_dict(self) -> dict[str, float]:
        return {
            "semantic": round(self.semantic, 6),
            "recency": round(self.recency, 6),
            "frequency": round(self.frequency, 6),
            "importance": round(self.importance, 6),
            "total": round(self.total, 6),
        }

    def explain(self) -> str:
        """One human-readable line: the arithmetic, not just the answer."""
        return (
            f"{WEIGHT_SEMANTIC}*{self.semantic:.4f}(sem) + "
            f"{WEIGHT_RECENCY}*{self.recency:.4f}(rec) + "
            f"{WEIGHT_FREQUENCY}*{self.frequency:.4f}(freq) + "
            f"{WEIGHT_IMPORTANCE}*{self.importance:.4f}(imp) = {self.total:.6f}"
        )


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """A candidate plus the score the ranker gave it.

    The score is carried *outside* `RetrievalCandidate` rather than written over
    its `.score` field for two reasons: `RetrievalCandidate.score` means "how
    well the retrieval paths matched the query" and overwriting it would destroy
    the input to `semantic_score()`; and the composer's drop policy must compare
    ranking scores, so they need to be unambiguous and immutable at that point.
    """

    candidate: RetrievalCandidate
    score: float
    breakdown: ScoreBreakdown

    @property
    def memory_id(self) -> str:
        return self.candidate.memory_id

    @property
    def content(self) -> str:
        return self.candidate.content

    def to_dict(self) -> dict:
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "score": round(self.score, 6),
            "breakdown": self.breakdown.to_dict(),
        }


# ---------------------------------------------------------------------------
# step 3 — scoring
# ---------------------------------------------------------------------------

def score_breakdown(
    candidate: RetrievalCandidate,
    now: datetime | None = None,
) -> ScoreBreakdown:
    """Compute the four signals and the weighted total, keeping both."""
    semantic = semantic_score(candidate)
    recency = recency_score(candidate, now)
    frequency = frequency_score(candidate)
    importance = importance_score(candidate)

    # The formula, spelled out. 0.4 / 0.2 / 0.2 / 0.2.
    total = (
        WEIGHT_SEMANTIC * semantic
        + WEIGHT_RECENCY * recency
        + WEIGHT_FREQUENCY * frequency
        + WEIGHT_IMPORTANCE * importance
    )

    return ScoreBreakdown(
        semantic=semantic,
        recency=recency,
        frequency=frequency,
        importance=importance,
        total=total,
    )


def score_candidate(
    candidate: RetrievalCandidate,
    now: datetime | None = None,
) -> float:
    """The weighted score for one candidate. Always a finite float in [0, 1].

    `now` is injectable so a caller ranking a batch scores every candidate
    against one instant — without it, a slow batch would decay later candidates
    against a marginally later clock and the ordering would depend on how long
    the loop took.
    """
    return score_breakdown(candidate, now).total


# ---------------------------------------------------------------------------
# step 4 — the node
# ---------------------------------------------------------------------------

def _sort_key(ranked: RankedCandidate) -> tuple[float, str]:
    """Score descending, then `memory_id` ascending.

    Rounding before comparison matters. Two candidates whose signals are
    identical can still differ in the last bit or two of the float — the sum is
    evaluated in the same order for both, so in practice they agree, but a
    difference of 1e-17 would silently override the `memory_id` tiebreaker and
    make the order depend on floating-point noise instead of on something
    stable. Twelve decimal places is far below any difference that could matter
    to a ranking and far above float noise.
    """
    return (-round(ranked.score, 12), ranked.memory_id)


def rank(
    candidates: Iterable[RetrievalCandidate],
    *,
    top_k: int | None = None,
    now: datetime | None = None,
) -> list[RankedCandidate]:
    """Score every candidate, sort descending, return the top `k`.

    `top_k` defaults to `RETRIEVE_RANKING_TOP_K` (5). `top_k=None` means "use the
    configured default"; pass a number to override, or a number >= len() to keep
    everything.

    An empty input returns an empty list — retrieval finding nothing is a normal
    outcome on a cold memory store, not an error.
    """
    items = list(candidates or [])
    if not items:
        return []

    # One clock for the whole batch: see `score_candidate`.
    reference = now or utc_now()

    ranked: list[RankedCandidate] = []
    for candidate in items:
        breakdown = score_breakdown(candidate, reference)
        ranked.append(
            RankedCandidate(
                candidate=candidate,
                score=breakdown.total,
                breakdown=breakdown,
            )
        )
    ranked.sort(key=_sort_key)

    limit = config.ranking_top_k() if top_k is None else int(top_k)
    if limit < 0:
        limit = 0
    selected = ranked[:limit]

    _log_breakdown(ranked, selected)
    return selected


# ---------------------------------------------------------------------------
# step 14 — inspectable ranking decisions
# ---------------------------------------------------------------------------

def _log_breakdown(
    ranked: Sequence[RankedCandidate],
    selected: Sequence[RankedCandidate],
) -> None:
    """Emit the per-candidate score breakdown, behind a flag.

    Two ways to turn it on, because the two audiences differ. `RETRIEVE_RANKING_DEBUG=1`
    raises it to INFO for someone tuning weights by hand who does not want the
    rest of the DEBUG firehose; otherwise it goes out at DEBUG like everything
    else. When neither is on, nothing is formatted at all — the `isEnabledFor`
    guard keeps this off the hot path rather than building strings the logging
    module will discard.
    """
    forced = config.ranking_debug()
    if not forced and not logger.isEnabledFor(logging.DEBUG):
        return

    level = logging.INFO if forced else logging.DEBUG
    kept = {item.memory_id for item in selected}
    logger.log(
        level,
        "ranking %d candidates, keeping %d (weights %s)",
        len(ranked),
        len(selected),
        RANKING_WEIGHTS,
    )
    for position, item in enumerate(ranked, start=1):
        logger.log(
            level,
            "  #%d %s %s  [%s]  %r",
            position,
            "KEEP" if item.memory_id in kept else "drop",
            item.memory_id,
            item.breakdown.explain(),
            item.content[:60],
        )
