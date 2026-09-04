"""M3 integration test — the eval harness end to end.

Invokes `evals/run_eval.py` the way the Definition of Done does, as a real
subprocess, and asserts the run produced genuine precision and recall numbers
rather than placeholders. Running it in-process would let an import-time side
effect or a leaked event loop make it pass where the documented command fails.

Run:  pytest tests/integration/test_eval_harness.py -v
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: The COMMITTED baseline. Read here, never written by this suite - see below.
RESULTS = ROOT / "evals" / "results" / "golden_set_v1.json"

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# WHY EVERY RUN IN THIS FILE PASSES `--out`
# ---------------------------------------------------------------------------
#
# `run_eval.py` writes `evals/results/<suite>.json` by default, and
# `DEFAULT_BASELINES` points the regression gate at
# `evals/results/golden_set_v1.json`. So a fixture that ran the v1 suite with no
# `--out` OVERWROTE the very file the gate compares against.
#
# That is not a tidiness problem, it defeats the gate. An independent verifier
# demonstrated the full loop: degrade retrieval, run `pytest tests/`, and the
# regenerated baseline records the degraded numbers; gate the equally-degraded
# v2 run against it and you get `+0.0000 ok`, GATE PASS, exit 0 - for a
# regression that exits 3 against the committed baseline. The project's own
# workflow (run the suite, commit the artifacts) wrote the regression into the
# baseline before the gate could read it.
#
# So: this suite is now NON-MUTATING. Every run writes to a temp directory, and
# assertions about "the file the runner wrote" read that temp copy. The
# committed baseline is only ever read - as the gate's reference, which is
# exactly what a baseline is for.


@pytest.fixture(scope="module")
def results_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A scratch directory for this module's eval artifacts."""
    return tmp_path_factory.mktemp("eval-results")


def _run_eval_to(out_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "evals/run_eval.py", *args, "--out", str(out_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            # The runner prints box-drawing and arrows; a cp1252 console would
            # raise UnicodeEncodeError and fail the run for a cosmetic reason.
            "PYTHONIOENCODING": "utf-8",
        },
        timeout=900,
    )


@pytest.fixture(scope="module")
def v1_out(results_dir: Path) -> Path:
    return results_dir / "golden_set_v1.json"


@pytest.fixture(scope="module")
def eval_run(v1_out: Path) -> subprocess.CompletedProcess:
    """Run the v1 suite as the Definition of Done specifies, minus the clobber."""
    return _run_eval_to(v1_out, "--suite", "golden_set_v1")


def _metric(stdout: str, label: str) -> float:
    match = re.search(rf"^\s*{label}\s*:\s*([0-9.]+)", stdout, re.MULTILINE)
    assert match, f"{label!r} was never printed by the eval runner:\n{stdout}"
    return float(match.group(1))


def test_run_eval_reports_precision_and_recall(eval_run):
    """The runner exits 0 and reports precision and recall both > 0."""
    assert eval_run.returncode == 0, (
        f"eval run failed (exit {eval_run.returncode})\n"
        f"--- stdout ---\n{eval_run.stdout}\n--- stderr ---\n{eval_run.stderr}"
    )

    precision = _metric(eval_run.stdout, "precision")
    recall = _metric(eval_run.stdout, "recall")

    assert precision > 0, f"precision must be strictly positive, got {precision}"
    assert recall > 0, f"recall must be strictly positive, got {recall}"
    assert 0 < precision <= 1 and 0 < recall <= 1

    # Per-path breakdown (plan step 13): both paths must be visibly represented,
    # otherwise the "hybrid" retriever is really running on one path.
    assert "PER-PATH BREAKDOWN" in eval_run.stdout
    both = _metric(eval_run.stdout, r"found by BOTH paths\s+")
    keyword_only = _metric(eval_run.stdout, r"found by KEYWORD only\s+")
    semantic_only = _metric(eval_run.stdout, r"found by SEMANTIC only\s+")
    assert both > 0, "no candidate was found by both paths — the merge never fired"
    assert keyword_only > 0, "the keyword path contributed nothing anywhere in the suite"
    assert semantic_only > 0, "the semantic path contributed nothing anywhere in the suite"

    assert "all path expectations met" in eval_run.stdout, (
        "a golden-set path expectation was not met:\n" + eval_run.stdout
    )


def test_broken_path_expectation_exits_non_zero(tmp_path):
    """A suite whose probes stopped discriminating must FAIL, not pass quietly.

    Raising SEMANTIC_TOP_K to 35 pulls the keyword-only target (semantic rank 31
    of 44) into the semantic path's results, so gs-002 is no longer keyword-only.
    The runner previously printed "PATH EXPECTATIONS NOT MET" and still exited 0,
    which meant the DoD command and any CI wiring saw a clean pass on a golden
    set that had stopped testing anything.
    """
    result = subprocess.run(
        [
            sys.executable, "evals/run_eval.py", "--suite", "golden_set_v1",
            "--out", str(tmp_path / "broken.json"),
        ],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
            "RETRIEVE_SEMANTIC_TOP_K": "35",
        },
    )

    assert result.returncode == 2, (
        f"broken suite exited {result.returncode}, expected 2\n{result.stdout}"
    )
    assert "PATH EXPECTATIONS NOT MET" in result.stdout
    assert "gs-002" in result.stdout
    assert "all path expectations met" not in result.stdout


def test_baseline_file_written_with_real_numbers(eval_run, v1_out):
    """The runner writes a payload of real numbers - the shape M8 gates against.

    Reads the RUN'S OWN output (`v1_out`), not the committed baseline. Asserting
    against the committed file would have been satisfied by a stale artifact
    nobody regenerated, and getting there required overwriting the gate's
    reference - see the note at the top of this file.
    """
    assert eval_run.returncode == 0, eval_run.stderr
    assert v1_out.exists(), f"{v1_out} was not written"

    payload = json.loads(v1_out.read_text(encoding="utf-8"))
    agg = payload["aggregate"]

    assert payload["suite"] == "golden_set_v1"
    assert agg["queries"] == len(payload["queries"]) > 0
    for key in ("precision", "recall", "f1", "macro_precision", "macro_recall",
                "mrr", "mean_precision_at_r"):
        assert isinstance(agg[key], (int, float)), f"{key} is not numeric: {agg[key]!r}"
    assert agg["precision"] > 0 and agg["recall"] > 0

    # The rank-sensitive metrics M8 should gate on, added alongside (never
    # instead of) precision/recall/f1.
    assert 0 < agg["mrr"] <= 1.0
    assert 0 < agg["mean_precision_at_r"] <= 1.0
    for q in payload["queries"]:
        assert "reciprocal_rank" in q and "precision_at_r" in q

    # macro precision is pinned by the label counts; assert the identity holds
    # so the caveat in the docs stays true rather than becoming folklore.
    assert agg["macro_precision"] == pytest.approx(
        agg["mean_expected_size"] / payload["top_k"], abs=1e-6
    ), "macro precision no longer equals mean(|expected|)/k — revisit the documented caveat"

    # Headline aliases must track the macro numbers — M8 reads `precision`/`recall`.
    assert agg["precision"] == agg["macro_precision"]
    assert agg["recall"] == agg["macro_recall"]

    # The numbers must be reproducible from the per-query records, so a human
    # can check the baseline by hand rather than trusting it.
    recomputed = sum(q["precision"] for q in payload["queries"]) / len(payload["queries"])
    assert recomputed == pytest.approx(agg["macro_precision"], abs=1e-6)

    assert payload["path_expectations_met"] is True, payload["path_expectation_failures"]

    expectations = {q["path_expectation"] for q in payload["queries"]}
    assert {"keyword_only", "semantic_only", "both"} <= expectations, (
        f"the golden set must carry all three path expectations, has {expectations}"
    )


# ===========================================================================
# M8 — golden_set_v2 and the regression gate
# ===========================================================================

#: The COMMITTED v1 baseline. READ-ONLY here — it is the gate's reference, and
#: this suite regenerating it is precisely the defect described at the top of
#: this file.
BASELINE_V1 = ROOT / "evals" / "results" / "golden_set_v1.json"


def _baseline_corpus_size() -> int:
    return json.loads(BASELINE_V1.read_text(encoding="utf-8"))["corpus_size"]


@pytest.fixture(scope="module")
def v2_out(results_dir: Path) -> Path:
    return results_dir / "golden_set_v2.json"


@pytest.fixture(scope="module")
def eval_run_v2(v2_out: Path) -> subprocess.CompletedProcess:
    """The v2 suite gated against the COMMITTED v1 baseline.

    `--baseline` is passed explicitly even though it is now the default, because
    this fixture is the one place the comparison being made must be unambiguous
    in the test's own source. Output goes to a temp file so the suite never
    rewrites a committed artifact.
    """
    return _run_eval_to(v2_out, "--suite", "golden_set_v2", "--baseline", str(BASELINE_V1))


def test_eval_v2_meets_or_exceeds_v1_baseline(eval_run_v2, v2_out):
    """The plan's M8 gate, implemented per harness.md's binding guidance.

    The comparison is v1's NINE QUERIES, held out inside v2, against v1's own
    baseline — not v2's blended aggregate. Two reasons, both recorded in
    harness.md before this was written:

    1. The v1 baseline is saturated: recall, MRR and P@R are all exactly 1.0.
       A `v2_blended >= v1` gate therefore demands perfection on the expanded
       suite, so any query hard enough to be worth adding would fail it. The
       plan asks M8 to expand the golden set AND meet-or-exceed the baseline;
       read literally, those two instructions are in direct conflict.
    2. Running the same nine queries against v2's 63-row corpus (v1 had 44) is
       itself a strictly harder condition, so this is a genuine no-regression
       test rather than a re-run of a suite already known to pass.

    v2's own new queries are deliberately NOT gated here — on their first run
    they have no baseline to regress against. They are asserted to be
    non-trivial by `test_v2_new_queries_are_not_saturated` instead.
    """
    assert eval_run_v2.returncode == 0, (
        f"v2 run exited {eval_run_v2.returncode}, expected 0\n"
        f"--- stdout ---\n{eval_run_v2.stdout[-4000:]}\n"
        f"--- stderr ---\n{eval_run_v2.stderr[-2000:]}"
    )
    assert "GATE PASS" in eval_run_v2.stdout
    assert "GATE FAIL" not in eval_run_v2.stdout

    payload = json.loads(v2_out.read_text(encoding="utf-8"))
    baseline = json.loads(BASELINE_V1.read_text(encoding="utf-8"))["aggregate"]
    held_out = payload["aggregate_by_tier"]["v1_holdout"]

    assert held_out["queries"] == baseline["queries"], (
        "the held-out tier no longer has the same number of queries as the "
        "baseline — the two sides of the gate are not comparable"
    )
    for metric in ("macro_recall", "mrr", "mean_precision_at_r"):
        assert held_out[metric] >= baseline[metric] - 1e-9, (
            f"{metric} regressed: {held_out[metric]} < {baseline[metric]}"
        )

    # The corpus really did grow — otherwise "a harder condition" is a claim
    # rather than a fact, and the gate is just v1 re-run under a new name.
    assert payload["corpus_size"] > _baseline_corpus_size(), (
        "v2 did not seed a larger corpus than v1; the gate proves nothing"
    )


def test_v2_new_queries_are_not_saturated(eval_run_v2, v2_out):
    """v2's new queries must be able to FAIL, or they measure nothing.

    The point of expanding the golden set was to escape v1's ceiling, where
    recall = MRR = P@R = 1.0 unanimously and every metric could only regress.
    If the new tier also scores a clean sweep, the suite got bigger without
    getting harder, and the next milestone inherits the same blind baseline
    this one was supposed to remove.
    """
    assert eval_run_v2.returncode == 0, eval_run_v2.stderr
    payload = json.loads(v2_out.read_text(encoding="utf-8"))
    new = payload["aggregate_by_tier"]["v2_new"]

    assert new["queries"] >= 8, "v2 added too few queries to characterise anything"

    # ---- the structural assertion, and the reason this test was rewritten ---
    #
    # The aggregate check below used to be the whole test, and two independent
    # verifications made the same criticism of it: the tier was scoring under
    # 1.0 only because two queries labelled 5 and 4 expected answers while the
    # retriever returns 5 results. That is ARITHMETIC, not difficulty. A suite
    # can satisfy an aggregate threshold purely by labelling more documents per
    # query, without a single hard question in it — the exact gameable property
    # `run_eval.py`'s own NOTE warns about for macro precision.
    #
    # So the real assertion is about SINGLE-ANSWER queries. With one expected
    # row, `reciprocal_rank < 1.0` can only mean the retriever ranked something
    # else above the right answer. No labelling choice can manufacture that.
    rows = payload["queries"]
    single_answer = [q for q in rows if q.get("tier") == "v2_new" and len(q["expected"]) == 1]
    misranked = [q for q in single_answer if q["reciprocal_rank"] < 1.0]

    assert len(misranked) >= 3, (
        "fewer than three single-answer v2 queries have their answer below rank 1, so this "
        "suite is not measuring ranking difficulty. Aggregate scores under 1.0 can be "
        "produced by labelling more answers per query than the retriever returns; only a "
        "single-answer query ranked below 1 proves something was preferred over the right "
        "row. Add queries whose obvious lexical match is deliberately WRONG — negation "
        "('which lens do I use when NOT shooting portraits') is this retriever's reliable "
        "blind spot, since naming a row to exclude it promotes it to first place. "
        f"Currently misranked: {[q['query_id'] for q in misranked]}"
    )

    # At least one answer should sit well down the list, not merely at rank 2.
    # A suite where every miss is off-by-one cannot distinguish a small ranking
    # change from a large one.
    deep = [q for q in single_answer if q["reciprocal_rank"] <= 0.25]
    assert deep, (
        "no single-answer v2 query has its answer at rank 4 or worse. Every miss is "
        "off-by-one, so the suite cannot tell a small ranking regression from a large "
        f"one. Ranks: {sorted(round(1 / q['reciprocal_rank']) for q in misranked)}"
    )

    # The aggregate, kept as a floor rather than as the primary signal.
    assert new["mean_precision_at_r"] < 1.0 or new["mrr"] < 1.0, (
        "every new v2 query scored perfectly, so the suite is bigger but not "
        f"harder: {new}."
    )
    # ...but not so hard that it is simply broken. A tier scoring near zero
    # means the labels are wrong, not that retrieval is bad.
    assert new["macro_recall"] > 0.5, f"v2 new-query recall implausibly low: {new}"


def test_v2_covers_the_lifecycle_states_the_plan_names(eval_run_v2, v2_out):
    """Decayed, archived, reflection and soft-deleted rows are all exercised.

    Plan step M8.13 asks for "queries covering decayed/archived memories,
    reflection summaries, and the deleted-never-resurfaces case". This asserts
    the suite contains all four rather than trusting the labels — a query
    tagged `archived` against a row nobody archived is exactly the vacuous pass
    this milestone exists to prevent.
    """
    assert eval_run_v2.returncode == 0, eval_run_v2.stderr
    payload = json.loads(v2_out.read_text(encoding="utf-8"))
    states = {q.get("lifecycle_state") for q in payload["queries"]}
    for required in ("archived", "decayed", "reflection", "soft-deleted"):
        assert required in states, f"no v2 query exercises a {required} row"

    # The archived and decayed rows must still be RETRIEVABLE. Archiving is not
    # erasure: retrieval filters on `deleted_at IS NULL` and nothing else. If
    # anyone later adds an `archived_at IS NULL` filter, this is the tripwire.
    for state in ("archived", "decayed"):
        row = next(q for q in payload["queries"] if q.get("lifecycle_state") == state)
        assert row["recall"] == 1.0, (
            f"the {state} row was not retrieved — has a {state}-aware filter been "
            f"added to the retrieval path? query {row['query_id']}"
        )


def test_soft_deleted_memory_never_resurfaces(eval_run_v2, v2_out):
    """The erased row must not come back, and the check must be non-vacuous.

    `forbidden_memory_ids` is scored over the whole returned ranking rather
    than the top-k slice: a deleted row at rank 9 has still been resurfaced, it
    just got lucky about where the cutoff happened to fall.
    """
    assert eval_run_v2.returncode == 0, eval_run_v2.stderr
    payload = json.loads(v2_out.read_text(encoding="utf-8"))
    assert payload["forbidden_respected"] is True, payload["forbidden_failures"]

    guarded = [q for q in payload["queries"] if q.get("forbidden_slugs")]
    assert guarded, "no query declares forbidden_memory_ids — the check is vacuous"
    for q in guarded:
        assert q["forbidden_resurfaced"] == [], q

    # Non-vacuity, which is the part that actually matters: the query has to
    # retrieve the deleted row's NEIGHBOURS. A query returning nothing at all
    # would also "not resurface" it, and would prove nothing about erasure.
    assert any(q["recall"] > 0 for q in guarded), (
        "the forbidden-id queries retrieved none of their expected rows, so the "
        "absence of the deleted row is indistinguishable from a dead retriever"
    )


def test_v2_corpus_does_not_break_the_keyword_only_probe(eval_run_v2, v2_out):
    """v2's new rows must not contain the lexeme gs-002 depends on.

    gs-002 is the suite's only keyword-only probe and it rests on a stemming
    collision: 'origin' and 'original' both stem to `origin`, so `content_tsv`
    matches the `tiles` row and — verified corpus-wide — nothing else. One
    careless "originally" among the new rows would quietly make the probe
    ambiguous, and the failure would look like a retrieval regression.

    `seed_memories.py` names this test in a comment as the thing that enforces
    the rule rather than trusting the comment. It now exists.
    """
    assert eval_run_v2.returncode == 0, eval_run_v2.stderr
    from evals.fixtures.seed_memories import V2_EXTRA

    offenders = [
        m.slug
        for m in V2_EXTRA
        if any(
            w.lower().startswith("origin")
            for w in re.findall(r"[A-Za-z]+", m.content)
        )
    ]
    assert not offenders, (
        f"v2 rows {offenders} contain a word stemming to 'origin', which makes "
        f"gs-002's keyword-only probe ambiguous"
    )

    # And the probe still behaved as labelled in the actual v2 run.
    payload = json.loads(v2_out.read_text(encoding="utf-8"))
    gs002 = next(q for q in payload["queries"] if q["query_id"] == "gs-002")
    assert gs002["path_expectation_met"], gs002
    assert gs002["target_found_via"] == ["keyword"], gs002


def test_runner_exits_three_on_a_regressed_baseline(tmp_path):
    """The gate's verdict must reach the process exit code.

    The unit tests prove the comparison logic; this proves the WIRING, which is
    the part that silently rots. It fabricates a baseline nobody can meet —
    every gated metric at 2.0 — so the run is guaranteed to regress without
    needing a broken retriever, and asserts exit 3 specifically: 1 (broken run)
    and 2 (broken suite) also mean failure, and they mean different things.
    """
    impossible = tmp_path / "impossible.json"
    impossible.write_text(
        json.dumps(
            {"aggregate": {"macro_recall": 2.0, "mrr": 2.0, "mean_precision_at_r": 2.0}}
        ),
        encoding="utf-8",
    )
    result = _run_eval_to(
        tmp_path / "regressed.json",
        "--suite", "golden_set_v2",
        "--no-seed",
        "--baseline", str(impossible),
    )
    assert result.returncode == 3, (
        f"regressed run exited {result.returncode}, expected 3\n{result.stdout[-3000:]}"
    )
    assert "GATE FAIL" in result.stdout
    assert "REGRESSED" in result.stdout
