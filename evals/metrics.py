"""Set-based retrieval metrics (plan step 11).

Deliberately tiny and dependency-free so the numbers in
`evals/results/golden_set_v1.json` can be recomputed by hand from the per-query
records — M8 compares against this baseline, and a baseline nobody can verify by
hand is not a baseline.

DEFINITIONS AND THEIR EDGE CASES
--------------------------------
Both metrics are computed on *sets*, after truncating the ranked retrieval to
the first `k` results (`precision@k` / `recall@k`):

    precision = |retrieved ∩ expected| / |retrieved|
    recall    = |retrieved ∩ expected| / |expected|
    f1        = harmonic mean

Degenerate denominators are resolved explicitly rather than left to raise:

  retrieved empty  -> precision 0.0. Retrieving nothing is not "perfectly
                      precise"; the convention of returning 1.0 here would let a
                      retriever that answers nothing score a perfect baseline.
  expected empty   -> recall 1.0. There was nothing to find, so nothing was
                      missed. (`precision` is still 0.0 for any non-empty
                      retrieval, which is the correct penalty.)
  both empty       -> precision 0.0, recall 1.0, f1 0.0.
  p + r == 0       -> f1 0.0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence


def _as_set(ids: Iterable[str]) -> set[str]:
    return {str(i) for i in ids}


def precision(retrieved: Iterable[str], expected: Iterable[str]) -> float:
    """|retrieved ∩ expected| / |retrieved|; 0.0 when nothing was retrieved."""
    retrieved_set = _as_set(retrieved)
    if not retrieved_set:
        return 0.0
    return len(retrieved_set & _as_set(expected)) / len(retrieved_set)


def recall(retrieved: Iterable[str], expected: Iterable[str]) -> float:
    """|retrieved ∩ expected| / |expected|; 1.0 when nothing was expected."""
    expected_set = _as_set(expected)
    if not expected_set:
        return 1.0
    return len(_as_set(retrieved) & expected_set) / len(expected_set)


def f1(p: float, r: float) -> float:
    """Harmonic mean of precision and recall; 0.0 when both are 0."""
    if p + r <= 0:
        return 0.0
    return 2 * p * r / (p + r)


# ---------------------------------------------------------------------------
# rank-sensitive metrics
# ---------------------------------------------------------------------------
#
# WHY THESE EXIST, and why precision@k alone is not enough.
#
# precision@k over a fixed k is structurally capped by the label counts. When
# the retriever always returns a full k results and recall is 1.0, precision
# reduces exactly to
#
#     macro_precision = mean(|expected_i|) / k
#
# which is a property of how many documents each query was labelled with, NOT
# of how well the retriever ranked them. On golden_set_v1 that is 12/9/5 =
# 0.2667 and it would stay 0.2667 however the results were ordered, as long as
# the right documents appeared somewhere in the top 5.
#
# That matters for M8, whose gate compares a v2 suite against this baseline:
#   * recall is already 1.0, so ">= baseline" demands perfection - one miss in
#     an expanded suite fails the gate.
#   * adding v2 queries that have a single expected document each drags
#     mean(|expected|) down and so drags macro precision down, failing the gate
#     even if retrieval genuinely improved.
#
# So the two metrics below are added ALONGSIDE precision/recall/f1 (which are
# left exactly as they were, because M8's comparison reads those keys). Both are
# sensitive to ORDER rather than to label counts:
#
#   MRR             mean reciprocal rank of the FIRST correct document. Rewards
#                   putting a right answer at the top. Unaffected by how many
#                   documents a query expects.
#   precision@R     precision measured at k = |expected| for that query
#                   ("R-precision"). Self-normalizing: a query expecting 3 docs
#                   is scored on its top 3, one expecting 1 on its top 1, so
#                   queries with different label counts are directly comparable
#                   and the structural cap disappears.


def reciprocal_rank(ranked: Sequence[str], expected: Iterable[str]) -> float:
    """1/rank of the first correct document; 0.0 if none appear.

    Takes the RANKED list, not a set — order is the whole point. Callers pass
    the top-k slice, not the full ranking: see `score_query` on why every metric
    shares one cutoff.
    """
    expected_set = _as_set(expected)
    if not expected_set:
        return 0.0
    for position, memory_id in enumerate(ranked, start=1):
        if str(memory_id) in expected_set:
            return 1.0 / position
    return 0.0


def precision_at_r(ranked: Sequence[str], expected: Iterable[str]) -> float:
    """R-precision: precision over the first |expected| results.

    Removes precision@k's dependence on the label count, so queries expecting
    one document and queries expecting three are on the same scale.
    """
    expected_set = _as_set(expected)
    if not expected_set:
        return 0.0
    r = len(expected_set)
    return len(_as_set(list(ranked)[:r]) & expected_set) / r


@dataclass(slots=True)
class QueryScore:
    query_id: str
    retrieved: list[str]
    expected: list[str]
    hits: list[str]
    precision: float
    recall: float
    f1: float
    reciprocal_rank: float = 0.0
    precision_at_r: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "retrieved": self.retrieved,
            "expected": self.expected,
            "hits": self.hits,
            "precision": round(self.precision, 6),
            "recall": round(self.recall, 6),
            "f1": round(self.f1, 6),
            "reciprocal_rank": round(self.reciprocal_rank, 6),
            "precision_at_r": round(self.precision_at_r, 6),
        }


def score_query(
    query_id: str,
    retrieved: Sequence[str],
    expected: Sequence[str],
    *,
    k: int | None = None,
) -> QueryScore:
    """Score one query. EVERY metric is measured at the same cutoff `k`.

    THE CUTOFF CONVENTION, because it has to be settled once
    ---------------------------------------------------------
    All five metrics see the same truncated list. An earlier version passed the
    *untruncated* ranking to `reciprocal_rank` / `precision_at_r` while giving
    `precision` / `recall` the top-k slice, which made the two families
    disagree about what "found" means. It was invisible on golden_set_v1
    because every hit there is at rank 1, but on a harder suite a hit at rank 8
    with k=5 would have reported `recall = 0.0` next to `MRR = 0.125` — one
    metric calling it a miss and the other calling it a find, in the same row.

    So: everything is "@k". `recall = 0` now implies `MRR = 0` and
    `precision_at_r = 0`, always. The cost is that MRR can only take values in
    {1, 1/2, ..., 1/k, 0}, which is the right trade — a metric that disagrees
    with its neighbours is worse than a metric with coarse resolution, and M8
    reads these keys side by side.

    Passing `k=None` scores the full list, and the convention still holds
    because then truncation is a no-op.
    """
    ranked = list(retrieved)
    truncated = ranked[:k] if k else ranked
    expected_list = [str(e) for e in expected]
    hits = [r for r in truncated if r in set(expected_list)]
    p = precision(truncated, expected_list)
    r = recall(truncated, expected_list)
    return QueryScore(
        query_id=query_id,
        retrieved=truncated,
        expected=expected_list,
        hits=hits,
        precision=p,
        recall=r,
        f1=f1(p, r),
        reciprocal_rank=reciprocal_rank(truncated, expected_list),
        precision_at_r=precision_at_r(truncated, expected_list),
    )


def aggregate(scores: Sequence[QueryScore]) -> dict[str, Any]:
    """Both aggregations, because they answer different questions.

    macro — the unweighted mean of the per-query metrics. Every query counts
            equally, so a single badly-served query is visible. This is the
            headline number and the one M8's regression gate reads.
    micro — pooled hits over pooled retrievals/expectations. Weighted by how
            many documents each query involves; less sensitive to a query with a
            tiny expected set.
    """
    if not scores:
        return {
            "queries": 0,
            "macro_precision": 0.0,
            "macro_recall": 0.0,
            "macro_f1": 0.0,
            "micro_precision": 0.0,
            "micro_recall": 0.0,
            "micro_f1": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
            "mrr": 0.0,
            "mean_precision_at_r": 0.0,
        }

    n = len(scores)
    macro_p = sum(s.precision for s in scores) / n
    macro_r = sum(s.recall for s in scores) / n
    macro_f = sum(s.f1 for s in scores) / n
    mrr = sum(s.reciprocal_rank for s in scores) / n
    mean_p_at_r = sum(s.precision_at_r for s in scores) / n

    total_hits = sum(len(set(s.retrieved) & set(s.expected)) for s in scores)
    total_retrieved = sum(len(set(s.retrieved)) for s in scores)
    total_expected = sum(len(set(s.expected)) for s in scores)
    micro_p = total_hits / total_retrieved if total_retrieved else 0.0
    micro_r = total_hits / total_expected if total_expected else 1.0
    micro_f = f1(micro_p, micro_r)

    return {
        "queries": n,
        "macro_precision": round(macro_p, 6),
        "macro_recall": round(macro_r, 6),
        "macro_f1": round(macro_f, 6),
        "micro_precision": round(micro_p, 6),
        "micro_recall": round(micro_r, 6),
        "micro_f1": round(micro_f, 6),
        # Rank-sensitive, label-count-independent. See the block comment above
        # `reciprocal_rank` for why precision@k alone cannot serve as M8's gate.
        "mrr": round(mrr, 6),
        "mean_precision_at_r": round(mean_p_at_r, 6),
        # Headline aliases — what M8's `--baseline` comparison reads. Do not
        # redefine these: M8 compares against the values already written into
        # evals/results/golden_set_v1.json.
        "precision": round(macro_p, 6),
        "recall": round(macro_r, 6),
        "f1": round(macro_f, 6),
        # Mean label count. Recorded so a reader can see WHY macro precision is
        # what it is: with a full top-k and recall 1.0, macro precision equals
        # exactly `mean_expected_size / k`. `run_eval` knows k and prints the
        # resulting cap alongside.
        "mean_expected_size": round(sum(len(set(s.expected)) for s in scores) / n, 6),
    }
