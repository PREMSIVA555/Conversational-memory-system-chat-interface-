"""The eval runner (plan steps 12, 13, 14).

    python evals/run_eval.py --suite golden_set_v1

Seeds the fixture corpus, runs the hybrid retriever over every labeled query,
prints per-query and aggregate precision/recall plus a per-path breakdown, and
writes the aggregate to `evals/results/<suite>.json` — the baseline M8's
regression gate compares against.

Exit codes:
    0  the suite ran to completion and every query behaved as it is labelled
    1  the run itself errored — a query raised, the DB was unreachable, the
       suite file was missing. A bad *score* is a finding; a failed *run* is a
       broken harness, and the two must not look alike to CI.
    2  the suite ran but a PATH EXPECTATION broke: a query labelled
       `keyword_only` was reached by the semantic path, or vice versa. The
       golden set's whole value is that those two probes discriminate between
       the paths, so a run where they stopped doing so must not report success
       — otherwise CI goes green on a retriever that has lost a path.
    3  the suite is intact but retrieval REGRESSED against its baseline. Kept
       apart from 2 deliberately: a broken suite is repaired by fixing the
       fixture, a regression must never be, and one shared non-zero code would
       let a regression be "fixed" by editing the golden set.

A bad score on a suite with NO baseline still exits 0. A suite that has one
(see `DEFAULT_BASELINES`) is gated by default; `--no-baseline` opts out. The
gate defaults to ON because the Definition of Done's own command passes no
`--baseline` yet requires the delta and pass/fail to be printed — with the gate
opt-in, the documented command printed no gate at all and the committed result
recorded `"gate": {"baseline": null, "passed": null}`.

WHICH METRIC TO ACTUALLY READ
-----------------------------
`precision` / `recall` / `f1` are kept exactly as they are because M8's
regression gate reads those keys — but precision@k here is structurally pinned
at `mean(|expected|)/k` whenever the retriever returns a full k and recall is
1.0, so it reports the golden set's label counts rather than the quality of the
ranking. MRR and mean P@R are reported alongside and are order-sensitive; those
are the numbers worth judging retrieval on. Full reasoning in `evals/metrics.py`.

WHY THE CACHE IS WARMED UP FRONT
--------------------------------
`retrieve.semantic` embeds one query per search. The Voyage account this runs on
is metered at 3 requests/minute, so nine sequential single-query embeddings is a
guaranteed 429 partway through the suite. The provider meters requests rather
than texts, so all nine queries are embedded in ONE batched call before any
retrieval starts. The vectors are real; only the request count changes. It also
makes the suite reproducible, which plan step 10 asks for.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Persist query vectors next to the corpus vectors so a rerun costs zero
# provider requests. Set RETRIEVE_EMBED_CACHE yourself to override.
os.environ.setdefault(
    "RETRIEVE_EMBED_CACHE",
    str(ROOT / "evals" / "fixtures" / "query_embedding_cache.json"),
)

from store.db import close_pools, ensure_selector_event_loop_policy  # noqa: E402

ensure_selector_event_loop_policy()

from evals import metrics  # noqa: E402
from evals.separation import measure_separation  # noqa: E402
from evals.fixtures.seed_memories import (  # noqa: E402
    GOLDEN_SET_ACTOR_ID,
    GOLDEN_SET_SUBJECT_ID,
    BY_ID,
    BY_ID_ALL,
    LIFECYCLE_V2,
    MEMORIES,
    V2_MEMORIES,
    seed,
)
from retrieve import config as retrieve_config  # noqa: E402
from retrieve.hybrid import hybrid_search  # noqa: E402
from retrieve.semantic import prune_persistent_cache, warm_query_cache  # noqa: E402
from retrieve.types import KEYWORD, SEMANTIC, RetrievalQuery  # noqa: E402

SUITES = {
    # suite name -> jsonl file
    "golden_set_v1": "golden_set.jsonl",
    "golden_set_v2": "golden_set_v2.jsonl",
}

#: suite name -> (corpus, lifecycle overrides). A suite has to seed its OWN
#: corpus: v2's queries ask about an archived row, a decayed row, a reflection
#: summary and a soft-deleted row, none of which exist in v1's 44 rows, and all
#: four of which are states applied AFTER insert. Seeding v1's corpus for a v2
#: run would leave those four queries silently measuring absent rows.
CORPORA = {
    "golden_set_v1": (MEMORIES, None),
    "golden_set_v2": (V2_MEMORIES, LIFECYCLE_V2),
}

#: The metrics M8's regression gate reads, in the order they are reported.
#:
#: `macro_precision` is deliberately ABSENT. It reduces to mean(|expected|)/k
#: whenever recall is 1.0 and the retriever returns a full k, so it reports the
#: golden set's label counts rather than the ranking - which makes it gameable
#: in both directions: adding single-answer queries fails it with a perfect
#: retriever, and labelling more documents per query passes it without touching
#: retrieval. It stays in the JSON payload for compatibility; it is not a gate.
GATE_METRICS = ("macro_recall", "mrr", "mean_precision_at_r")

#: suite -> the results file it is gated against BY DEFAULT.
#:
#: This exists because the Definition of Done's command is
#: `python evals/run_eval.py --suite golden_set_v2`, with no `--baseline`, and
#: that same line requires the runner to print "the explicit delta and pass/fail
#: against the baseline". With the gate opt-in those two halves contradicted
#: each other: the documented command printed no gate at all, and the committed
#: `golden_set_v2.json` recorded `"gate": {"baseline": null, "passed": null}` -
#: the milestone's own artifact proving the gate had never run.
#:
#: So a suite that HAS a baseline is gated unless you opt out with
#: `--no-baseline`. A gate you have to remember to switch on is not a gate.
DEFAULT_BASELINES = {
    "golden_set_v2": "golden_set_v1.json",
}


def resolve_baseline(suite: str, explicit: Path | None, disabled: bool) -> Path | None:
    """Pick the baseline for this run. Explicit beats default; --no-baseline wins."""
    if disabled:
        return None
    if explicit is not None:
        return explicit
    name = DEFAULT_BASELINES.get(suite)
    if name is None:
        return None
    path = ROOT / "evals" / "results" / name
    # A missing default baseline is not an error: the very first v1 run has to
    # be able to produce one. An explicitly named missing file IS an error, and
    # `load_baseline` raises for it.
    return path if path.exists() else None


#: The tier whose queries the gate is computed over. v2 carries every v1 query
#: unchanged, so the honest no-regression comparison is v1's queries against
#: v1's baseline - run over v2's LARGER corpus, which is a harder condition than
#: v1 itself faced. v2's own new queries are reported as characterization: they
#: have no baseline to regress against on their first run, and gating on them
#: would punish M8 for the harder queries the plan asks it to add.
GATE_TIER = "v1_holdout"

DEFAULT_TOP_K = 5


def resolve_suite(suite: str) -> Path:
    """Map a suite name to its jsonl, tolerating a literal filename too."""
    candidates = []
    if suite in SUITES:
        candidates.append(ROOT / "evals" / SUITES[suite])
    candidates.append(ROOT / "evals" / f"{suite}.jsonl")
    candidates.append(Path(suite))
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(
        f"unknown suite {suite!r}; known suites: {', '.join(sorted(SUITES))}"
    )


def load_suite(path: Path) -> list[dict]:
    records = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno} is not valid JSON: {exc}") from exc
    if not records:
        raise ValueError(f"{path} contains no records")
    return records


def all_suite_queries() -> list[str]:
    """Every query text across every registered suite, de-duplicated.

    The union the query cache is pruned against. A suite whose file is missing
    is skipped rather than raising: `SUITES` is the registry of suites that
    *may* exist, and a partial checkout should not make an otherwise valid run
    fail on a file it never needed.

    v2 contains all nine v1 queries verbatim, so the union is v2's list — but
    that is a fact about today's suites, not something to rely on.
    """
    seen: dict[str, None] = {}
    for name in SUITES:
        try:
            path = resolve_suite(name)
        except FileNotFoundError:
            continue
        for record in load_suite(path):
            query = record.get("query")
            if isinstance(query, str) and query:
                seen.setdefault(query, None)
    return list(seen)


def _slug(memory_id: str) -> str:
    # BY_ID_ALL, not BY_ID: a v2 run retrieves v2-only rows, and labelling them
    # by a uuid prefix would make the near-miss distractors unreadable in the
    # output - which is the one place you actually diagnose a ranking.
    mem = BY_ID_ALL.get(memory_id)
    return mem.slug if mem else memory_id[:8]


# ---------------------------------------------------------------------------
# the regression gate (plan step M8.14)
# ---------------------------------------------------------------------------

#: Exit code for "the suite ran, but it regressed against the baseline".
EXIT_REGRESSION = 3

#: Floating-point slack. Metrics are aggregated as sums of floats, so a run that
#: is arithmetically identical to the baseline can differ in the last bit. The
#: gate must not fail on 1e-16; it must fail on a real drop.
GATE_TOLERANCE = 1e-9


def _relative_to_root(path: Path | None) -> str | None:
    """Render a path relative to the repo root when it lives inside it."""
    if path is None:
        return None
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def load_baseline(path: Path) -> dict:
    """Read a previous run's JSON payload. Raises if it is not one."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"baseline {path} does not exist - run the v1 suite first: "
            f"python evals/run_eval.py --suite golden_set_v1"
        ) from exc
    if "aggregate" not in payload:
        raise ValueError(f"{path} is not an eval result payload (no 'aggregate' key)")
    return payload


def compare_to_baseline(
    current: dict,
    baseline: dict,
    *,
    metrics_to_gate: tuple[str, ...] = GATE_METRICS,
    tolerance: float = GATE_TOLERANCE,
) -> tuple[bool, list[dict]]:
    """Compare two aggregates. Returns (passed, one row per gated metric).

    Pure and DB-free on purpose: `test_eval_exits_nonzero_on_regression` feeds
    it a synthetic below-baseline aggregate, and a gate that needed a live
    retriever to test would only ever be exercised on the happy path.

    A metric missing from `current` is a FAILURE, not a skip. The alternative -
    treating absence as "nothing to compare" - means a renamed metric key turns
    the gate off silently, which is the exact failure mode a gate exists to
    prevent.
    """
    rows: list[dict] = []
    passed = True
    for name in metrics_to_gate:
        base_value = baseline.get(name)
        cur_value = current.get(name)
        if base_value is None:
            rows.append({"metric": name, "baseline": None, "current": cur_value,
                         "delta": None, "status": "no_baseline"})
            continue
        if cur_value is None:
            rows.append({"metric": name, "baseline": base_value, "current": None,
                         "delta": None, "status": "MISSING"})
            passed = False
            continue
        delta = cur_value - base_value
        regressed = delta < -tolerance
        if regressed:
            passed = False
        rows.append({
            "metric": name,
            "baseline": base_value,
            "current": cur_value,
            "delta": delta,
            "status": "REGRESSED" if regressed else "ok",
        })
    return passed, rows


def format_gate(rows: list[dict], *, tier: str, baseline_path: Path) -> str:
    """Render the gate table. Always prints the explicit delta (DoD line 6)."""
    lines = [
        f"REGRESSION GATE - {tier!r} queries vs {baseline_path.name}",
        f"    {'metric':<22}{'baseline':>12}{'current':>12}{'delta':>12}   status",
    ]
    for r in rows:
        base = "-" if r["baseline"] is None else f"{r['baseline']:.4f}"
        cur = "-" if r["current"] is None else f"{r['current']:.4f}"
        delta = "-" if r["delta"] is None else f"{r['delta']:+.4f}"
        lines.append(f"    {r['metric']:<22}{base:>12}{cur:>12}{delta:>12}   {r['status']}")
    return chr(10).join(lines)


async def run(
    suite: str,
    top_k: int,
    do_seed: bool,
    out_path: Path | None,
    baseline_path: Path | None = None,
) -> int:
    suite_path = resolve_suite(suite)
    records = load_suite(suite_path)
    corpus, lifecycle = CORPORA.get(suite, (MEMORIES, None))

    print("=" * 78)
    # Plain ASCII: this line lands on a Windows console whose default code page
    # is cp1252, and an em-dash renders there as a replacement character.
    print(f"  retrieval eval - suite {suite!r}  ({len(records)} queries, top_k={top_k})")
    print(f"  corpus subject_id: {GOLDEN_SET_SUBJECT_ID}")
    print("=" * 78)

    if do_seed:
        summary = await seed(memories=corpus, lifecycle=lifecycle)
        print(
            f"seeded: {summary['inserted']} memories "
            f"(replaced {summary['deleted']}), embedding model {summary['embedding_model']}"
        )
        if summary.get("lifecycle_applied"):
            states = ", ".join(f"{k}={'/'.join(v)}" for k, v in (lifecycle or {}).items())
            print(
                f"lifecycle: {summary['lifecycle_applied']} row(s) aged after insert "
                f"[{states}]"
            )
    else:
        print("seeding skipped (--no-seed)")

    suite_queries = [r["query"] for r in records]
    warmed = await warm_query_cache(suite_queries)
    # Prune against EVERY suite's queries, not just this run's.
    #
    # The purpose is unchanged: editing a query orphans its old vector (the
    # cache key is a hash of the text), so without pruning the file accumulates
    # embeddings of retired probes.
    #
    # But pruning against `suite_queries` alone made each suite evict the
    # other's vectors — measured, a v1 run took the query cache from 19 entries
    # to 9, and the next v2 run reported "10 newly embedded in one batched
    # request", spending a live Voyage request to recover what it had just
    # thrown away. On a 3-request-per-minute key that is silent: nothing fails,
    # the run is merely slower.
    #
    # This is the same defect that was fixed in `seed()` for the CORPUS cache
    # via `all_corpus_texts()`; an independent verifier found the query cache
    # still had it. Both caches now prune against the union.
    orphans = prune_persistent_cache(all_suite_queries())
    print(
        f"query embeddings: {warmed} newly embedded in one batched request, rest cached"
        + (f"; pruned {orphans} orphaned vector(s)" if orphans else "")
    )
    print()

    # The separation margin is measured against the SEMANTIC path's own cutoff,
    # not the eval's top_k — it answers "did the semantic path reach this row?",
    # which is decided by SEMANTIC_TOP_K.
    semantic_k = retrieve_config.semantic_top_k()

    scores: list[metrics.QueryScore] = []
    per_query_rows: list[dict] = []
    totals = {"semantic_only": 0, "keyword_only": 0, "both": 0}
    expectation_failures: list[str] = []
    forbidden_failures: list[str] = []
    tier_of: dict[str, str] = {}

    started = time.perf_counter()
    for record in records:
        query_id = record["query_id"]
        result = await hybrid_search(
            RetrievalQuery(
                text=record["query"],
                subject_id=GOLDEN_SET_SUBJECT_ID,
                actor_id=GOLDEN_SET_ACTOR_ID,
            )
        )
        ranked = [c.memory_id for c in result.candidates]
        score = metrics.score_query(
            query_id, ranked, record["expected_memory_ids"], k=top_k
        )
        scores.append(score)
        tier_of[query_id] = record.get("tier", "v1_holdout")

        # ---- per-path breakdown (plan step 13) --------------------------
        kept = result.candidates[:top_k]
        both = [c for c in kept if len(c.paths) > 1]
        sem_only = [c for c in kept if c.paths == {SEMANTIC}]
        kw_only = [c for c in kept if c.paths == {KEYWORD}]
        totals["both"] += len(both)
        totals["semantic_only"] += len(sem_only)
        totals["keyword_only"] += len(kw_only)

        # ---- forbidden ids: the deleted-never-resurfaces constraint -----
        # Scored over the WHOLE returned ranking, not the top-k slice. A
        # soft-deleted row that comes back at rank 9 has still been resurfaced;
        # it just got lucky about where the cutoff fell.
        forbidden = set(record.get("forbidden_memory_ids", []))
        resurfaced = [m for m in ranked if m in forbidden]
        if resurfaced:
            forbidden_failures.append(
                f"{query_id}: {[_slug(m) for m in resurfaced]} must never be "
                f"retrieved (soft-deleted) but came back at rank(s) "
                f"{[ranked.index(m) + 1 for m in resurfaced]}"
            )

        expectation = record.get("path_expectation", "any")
        contributed = set().union(*(c.paths for c in kept)) if kept else set()
        hit_paths: set[str] = set()
        for cand in kept:
            if cand.memory_id in set(record["expected_memory_ids"]):
                hit_paths |= cand.paths

        ok = True
        if expectation == "keyword_only":
            ok = KEYWORD in hit_paths and SEMANTIC not in hit_paths
        elif expectation == "semantic_only":
            ok = SEMANTIC in hit_paths and KEYWORD not in hit_paths
        elif expectation == "both":
            ok = hit_paths >= {SEMANTIC, KEYWORD}
        if not ok:
            expectation_failures.append(
                f"{query_id}: expected {expectation}, target(s) actually found via "
                f"{sorted(hit_paths) or 'nothing'}"
            )

        tier_tag = record.get("tier", "v1_holdout")
        state = record.get("lifecycle_state")
        print(
            f"[{query_id}] {record['query']!r}"
            f"   ({tier_tag}{'' if not state else ', ' + state + ' row'})"
        )
        print(f"    expectation : {expectation}{'' if ok else '   <-- NOT MET'}")
        print(f"    expected    : {[_slug(m) for m in record['expected_memory_ids']]}")
        print(f"    retrieved@{top_k} : {[_slug(m) for m in score.retrieved]}")
        print(
            f"    paths       : semantic={result.path_counts.get(SEMANTIC, 0)} "
            f"keyword={result.path_counts.get(KEYWORD, 0)}  |  "
            f"in top-{top_k}: both={len(both)} semantic-only={len(sem_only)} "
            f"keyword-only={len(kw_only)}"
        )
        print(f"    target via  : {sorted(hit_paths) or '-'}")

        # For the two single-path probes, report WHERE the excluded path put the
        # target in the full corpus ranking, and by how much it missed. A
        # membership check at one k cannot distinguish a 0.0013 margin from a
        # 0.13 one; this is the number that says whether the probe is robust or
        # a coin flip. See evals/separation.py.
        separation = None
        if expectation in ("keyword_only", "semantic_only"):
            separation = await measure_separation(
                record["query"],
                record["expected_memory_ids"],
                subject_id=GOLDEN_SET_SUBJECT_ID,
                actor_id=GOLDEN_SET_ACTOR_ID,
                boundary_rank=semantic_k,
            )
            if expectation == "keyword_only":
                print(f"    separation  : {separation.summary()}  [semantic must MISS]")
            else:
                print(
                    f"    separation  : semantic rank "
                    f"{separation.target_rank}/{separation.corpus_size} "
                    f"[semantic must FIND]"
                )
        print(
            f"    precision={score.precision:.3f}  recall={score.recall:.3f}  "
            f"f1={score.f1:.3f}   ({result.elapsed_ms:.0f}ms)"
        )
        if result.degraded:
            print(f"    DEGRADED    : {result.degraded}")
        print()

        per_query_rows.append(
            {
                **score.to_dict(),
                "query": record["query"],
                "expected_slugs": [_slug(m) for m in record["expected_memory_ids"]],
                "retrieved_slugs": [_slug(m) for m in score.retrieved],
                "path_expectation": expectation,
                "path_expectation_met": ok,
                "tier": record.get("tier", "v1_holdout"),
                "lifecycle_state": record.get("lifecycle_state"),
                "forbidden_slugs": record.get("forbidden_slugs", []),
                "forbidden_resurfaced": [_slug(m) for m in resurfaced],
                "target_found_via": sorted(hit_paths),
                "path_counts": result.path_counts,
                "contributed_paths": sorted(contributed),
                "degraded": result.degraded,
                "elapsed_ms": round(result.elapsed_ms, 2),
                "separation": separation.to_dict() if separation else None,
            }
        )

    elapsed = time.perf_counter() - started
    agg = metrics.aggregate(scores)

    # Split by tier. The gate reads the v1 subset; the new queries are reported
    # beside it as characterization. Reporting only the blended aggregate would
    # hide exactly what this suite was built to show - v2's new queries are
    # harder, so a blended number falls even when nothing regressed.
    by_tier = {
        tier: metrics.aggregate([sc for sc in scores if tier_of[sc.query_id] == tier])
        for tier in sorted({t for t in tier_of.values()})
        if any(tier_of[sc.query_id] == tier for sc in scores)
    }

    print("-" * 78)
    print("PER-PATH BREAKDOWN (candidates inside top-k, summed over all queries)")
    print(f"    found by BOTH paths        : {totals['both']}")
    print(f"    found by SEMANTIC only     : {totals['semantic_only']}")
    print(f"    found by KEYWORD only      : {totals['keyword_only']}")
    print("-" * 78)
    print("AGGREGATE")
    print(f"    queries        : {agg['queries']}")
    print(f"    precision      : {agg['macro_precision']:.4f}   (macro)")
    print(f"    recall         : {agg['macro_recall']:.4f}   (macro)")
    print(f"    f1             : {agg['macro_f1']:.4f}   (macro)")
    print(f"    micro_precision: {agg['micro_precision']:.4f}")
    print(f"    micro_recall   : {agg['micro_recall']:.4f}")
    print(f"    micro_f1       : {agg['micro_f1']:.4f}")
    print(f"    MRR            : {agg['mrr']:.4f}   (rank-sensitive)")
    print(f"    mean P@R       : {agg['mean_precision_at_r']:.4f}   (rank-sensitive)")
    cap = agg["mean_expected_size"] / top_k
    print(
        f"    NOTE: with a full top-{top_k} and recall {agg['macro_recall']:.2f}, macro precision"
        f"\n          is structurally pinned at mean(|expected|)/k = "
        f"{agg['mean_expected_size']:.4f}/{top_k} = {cap:.4f}. It measures the"
        f"\n          label counts, not the ranking. Use MRR / P@R to judge quality."
    )
    saturated = [
        name
        for name, value in (
            ("recall", agg["macro_recall"]),
            ("MRR", agg["mrr"]),
            ("mean P@R", agg["mean_precision_at_r"]),
        )
        if value >= 1.0
    ]
    if saturated:
        # A metric pinned at 1.0 cannot show improvement, only regression. M8's
        # gate is "v2 >= v1", so every saturated metric silently becomes a
        # demand for perfection on the expanded suite. Say so here rather than
        # letting whoever writes v2 discover it from a red build.
        print(
            f"    SATURATED      : {', '.join(saturated)} at ceiling on this suite."
            "\n          These can only regress, never improve, so M8's >= gate reads as"
            "\n          'do not regress'. golden_set_v2 needs harder queries — near-miss"
            "\n          distractors, multi-hop phrasing, decayed/archived rows — if the"
            "\n          baseline is to discriminate rather than merely tripwire."
        )
    print(f"    wall time      : {elapsed:.2f}s")
    print("-" * 78)

    if len(by_tier) > 1:
        print("BY TIER")
        for tier, tagg in by_tier.items():
            role = "GATED against baseline" if tier == GATE_TIER else "characterization only"
            print(
                f"    {tier:<12} n={tagg['queries']:<3} "
                f"recall={tagg['macro_recall']:.4f}  mrr={tagg['mrr']:.4f}  "
                f"P@R={tagg['mean_precision_at_r']:.4f}   [{role}]"
            )
        print("-" * 78)

    # ---- the regression gate (plan step M8.14) --------------------------
    gate_rows: list[dict] = []
    gate_passed = True
    if baseline_path is not None:
        baseline = load_baseline(baseline_path)
        base_agg = baseline["aggregate"]
        # Compare like with like: the baseline was computed over v1's nine
        # queries, so the current side must be those same nine, not the blend.
        current_agg = by_tier.get(GATE_TIER, agg)
        gate_passed, gate_rows = compare_to_baseline(current_agg, base_agg)
        print(format_gate(gate_rows, tier=GATE_TIER, baseline_path=baseline_path))
        if gate_passed:
            gated_n = by_tier.get(GATE_TIER, agg)["queries"]
            print(
                f"\n    GATE PASS - the {gated_n} held-out v1 queries hold their "
                f"baseline against a corpus of {len(corpus)} rows"
                f"\n    (the baseline was measured over "
                f"{baseline.get('corpus_size', '?')})."
            )
        else:
            print(
                "\n    GATE FAIL - retrieval regressed on the held-out v1 queries."
                "\n    These are the SAME nine queries the baseline was measured on, so"
                "\n    this is a real drop in retrieval quality, not an artefact of new"
                "\n    labels."
            )
        print("-" * 78)

    if forbidden_failures:
        print("FORBIDDEN MEMORIES RESURFACED:")
        for line in forbidden_failures:
            print(f"    - {line}")
        print(
            "\n    A soft-deleted memory was returned by retrieval. This is the single"
            "\n    worst failure this suite can report: the user asked for erasure and"
            "\n    the system kept answering with the erased row. It exits non-zero as a"
            "\n    broken suite, not as a low score."
        )
        print("-" * 78)
    if expectation_failures:
        print("PATH EXPECTATIONS NOT MET:")
        for line in expectation_failures:
            print(f"    - {line}")
        print(
            "\n    A golden-set query no longer behaves the way it is labelled. That is a"
            "\n    BROKEN SUITE, not a low score, so this run exits non-zero: the keyword-only"
            "\n    and semantic-only probes are the whole point of the set, and a suite where"
            "\n    they silently stopped discriminating would let CI pass on a retriever that"
            "\n    had lost a path entirely."
        )
    else:
        print("all path expectations met "
              "(keyword-only found only by keyword, semantic-only only by semantic)")
    print("-" * 78)

    payload = {
        "suite": suite,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "top_k": top_k,
        "subject_id": GOLDEN_SET_SUBJECT_ID,
        "corpus_size": len(corpus),
        "aggregate": agg,
        "aggregate_by_tier": by_tier,
        "gate": {
            # Repo-relative: an absolute path here is machine-specific noise in
            # a committed artifact, and makes two runs on two machines differ
            # in a field that says nothing about retrieval.
            "baseline": _relative_to_root(baseline_path),
            "tier": GATE_TIER,
            "metrics": list(GATE_METRICS),
            "passed": gate_passed if baseline_path else None,
            "rows": gate_rows,
        },
        "per_path_totals": totals,
        "path_expectations_met": not expectation_failures,
        "path_expectation_failures": expectation_failures,
        "forbidden_respected": not forbidden_failures,
        "forbidden_failures": forbidden_failures,
        "queries": per_query_rows,
    }

    out = out_path or (ROOT / "evals" / "results" / f"{suite}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote baseline -> {out}")

    # Exit codes are a contract:
    #   0 = suite ran, every query behaved as labelled, nothing regressed
    #   1 = the run itself errored (raised in main())
    #   2 = a path expectation broke, or a soft-deleted row resurfaced - either
    #       way the suite stopped measuring what it claims to measure
    #   3 = the suite is intact but retrieval REGRESSED against the baseline
    #
    # 2 and 3 are kept apart deliberately. A broken suite and a genuine quality
    # drop need different responses - the first is fixed by repairing the
    # fixture, the second must never be fixed that way - and a single non-zero
    # code would let someone "fix" a regression by editing the golden set.
    if expectation_failures or forbidden_failures:
        return 2
    if not gate_passed:
        return EXIT_REGRESSION
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a retrieval eval suite.")
    parser.add_argument("--suite", default="golden_set_v1", help="suite name (see SUITES)")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="cutoff for precision/recall")
    parser.add_argument("--no-seed", action="store_true", help="skip reseeding the fixture corpus")
    parser.add_argument("--out", type=Path, default=None, help="override the results path")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="a previous run's JSON; gate this run's v1 queries against it and "
             "exit 3 if recall/MRR/P@R regressed. Defaults per suite, see "
             "DEFAULT_BASELINES",
    )
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help="skip the regression gate even when the suite has a default baseline",
    )
    args = parser.parse_args(argv)
    baseline = resolve_baseline(args.suite, args.baseline, args.no_baseline)

    async def _run() -> int:
        try:
            return await run(
                args.suite, args.top_k, not args.no_seed, args.out, baseline
            )
        finally:
            await close_pools()

    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - a failed RUN must exit non-zero
        print(f"\nEVAL RUN FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
