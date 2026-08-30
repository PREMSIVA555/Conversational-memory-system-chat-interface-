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

A bad score still exits 0. Only a broken run (1) or a broken suite (2) fails.

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
    seed,
)
from retrieve import config as retrieve_config  # noqa: E402
from retrieve.hybrid import hybrid_search  # noqa: E402
from retrieve.semantic import prune_persistent_cache, warm_query_cache  # noqa: E402
from retrieve.types import KEYWORD, SEMANTIC, RetrievalQuery  # noqa: E402

SUITES = {
    # suite name -> jsonl file. M8 adds "golden_set_v2": "golden_set_v2.jsonl".
    "golden_set_v1": "golden_set.jsonl",
}

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


def _slug(memory_id: str) -> str:
    mem = BY_ID.get(memory_id)
    return mem.slug if mem else memory_id[:8]


async def run(suite: str, top_k: int, do_seed: bool, out_path: Path | None) -> int:
    suite_path = resolve_suite(suite)
    records = load_suite(suite_path)

    print("=" * 78)
    # Plain ASCII: this line lands on a Windows console whose default code page
    # is cp1252, and an em-dash renders there as a replacement character.
    print(f"  retrieval eval - suite {suite!r}  ({len(records)} queries, top_k={top_k})")
    print(f"  corpus subject_id: {GOLDEN_SET_SUBJECT_ID}")
    print("=" * 78)

    if do_seed:
        summary = await seed()
        print(
            f"seeded: {summary['inserted']} memories "
            f"(replaced {summary['deleted']}), embedding model {summary['embedding_model']}"
        )
    else:
        print("seeding skipped (--no-seed)")

    suite_queries = [r["query"] for r in records]
    warmed = await warm_query_cache(suite_queries)
    # Keep the on-disk query cache a faithful record of THIS suite. Editing a
    # query orphans its old vector (the cache key is a hash of the text), so
    # without this the file accumulates embeddings of retired probes.
    orphans = prune_persistent_cache(suite_queries)
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

        # ---- per-path breakdown (plan step 13) --------------------------
        kept = result.candidates[:top_k]
        both = [c for c in kept if len(c.paths) > 1]
        sem_only = [c for c in kept if c.paths == {SEMANTIC}]
        kw_only = [c for c in kept if c.paths == {KEYWORD}]
        totals["both"] += len(both)
        totals["semantic_only"] += len(sem_only)
        totals["keyword_only"] += len(kw_only)

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

        print(f"[{query_id}] {record['query']!r}")
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
        "corpus_size": len(BY_ID),
        "aggregate": agg,
        "per_path_totals": totals,
        "path_expectations_met": not expectation_failures,
        "path_expectation_failures": expectation_failures,
        "queries": per_query_rows,
    }

    out = out_path or (ROOT / "evals" / "results" / f"{suite}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote baseline -> {out}")

    # Exit codes are a contract: 0 = suite ran and every query behaved as
    # labelled, 2 = suite ran but a path expectation broke, 1 = the run itself
    # errored (raised in main()). A bad *score* is a finding and still exits 0;
    # a suite whose probes stopped discriminating is a failure.
    return 2 if expectation_failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a retrieval eval suite.")
    parser.add_argument("--suite", default="golden_set_v1", help="suite name (see SUITES)")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="cutoff for precision/recall")
    parser.add_argument("--no-seed", action="store_true", help="skip reseeding the fixture corpus")
    parser.add_argument("--out", type=Path, default=None, help="override the results path")
    args = parser.parse_args(argv)

    async def _run() -> int:
        try:
            return await run(args.suite, args.top_k, not args.no_seed, args.out)
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
