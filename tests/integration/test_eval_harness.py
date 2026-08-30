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
RESULTS = ROOT / "evals" / "results" / "golden_set_v1.json"

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def eval_run() -> subprocess.CompletedProcess:
    """Run the suite exactly as the Definition of Done specifies."""
    return subprocess.run(
        [sys.executable, "evals/run_eval.py", "--suite", "golden_set_v1"],
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
        timeout=600,
    )


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


def test_baseline_file_written_with_real_numbers(eval_run):
    """`evals/results/golden_set_v1.json` is the file M8 regresses against."""
    assert eval_run.returncode == 0, eval_run.stderr
    assert RESULTS.exists(), f"{RESULTS} was not written"

    payload = json.loads(RESULTS.read_text(encoding="utf-8"))
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
