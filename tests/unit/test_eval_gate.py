"""M8 unit tests — the regression gate's comparison logic.

The gate decides whether a retrieval change is allowed to land. These tests
drive `compare_to_baseline` directly with synthetic aggregates instead of
running a suite, for one reason: a gate exercised only by real runs is only
ever exercised on the happy path, and the case that matters — a genuine
regression — is exactly the one a passing system never produces.

Run:  pytest tests/unit/test_eval_gate.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.run_eval import (  # noqa: E402
    EXIT_REGRESSION,
    GATE_METRICS,
    GATE_TOLERANCE,
    compare_to_baseline,
    load_baseline,
)

#: A perfect aggregate — what the saturated v1 baseline actually looks like.
PERFECT = {"macro_recall": 1.0, "mrr": 1.0, "mean_precision_at_r": 1.0}


def test_identical_aggregates_pass():
    passed, rows = compare_to_baseline(dict(PERFECT), dict(PERFECT))
    assert passed
    assert [r["status"] for r in rows] == ["ok", "ok", "ok"]
    assert all(r["delta"] == 0.0 for r in rows)


@pytest.mark.parametrize("metric", GATE_METRICS)
def test_eval_exits_nonzero_on_regression(metric):
    """A below-baseline result on ANY gated metric fails the gate.

    This is the plan's `test_eval_exits_nonzero_on_regression`. It is
    parametrised over every gated metric because a gate that only watches
    recall would let a pure *ordering* regression through — the retriever still
    finds the row, just at rank 4 instead of rank 1, which recall cannot see
    and MRR can.
    """
    current = dict(PERFECT)
    current[metric] = PERFECT[metric] - 0.05

    passed, rows = compare_to_baseline(current, dict(PERFECT))

    assert not passed, f"a 0.05 drop in {metric} did not fail the gate"
    regressed = [r for r in rows if r["status"] == "REGRESSED"]
    assert [r["metric"] for r in regressed] == [metric]
    assert regressed[0]["delta"] == pytest.approx(-0.05)


def test_improvement_passes_and_is_reported_as_positive_delta():
    """Above baseline is a pass. The gate is `>=`, not `==`."""
    current = {**PERFECT, "mrr": 1.0, "macro_recall": 1.0, "mean_precision_at_r": 1.0}
    baseline = {**PERFECT, "mean_precision_at_r": 0.90}
    passed, rows = compare_to_baseline(current, baseline)
    assert passed
    par = next(r for r in rows if r["metric"] == "mean_precision_at_r")
    assert par["delta"] == pytest.approx(0.10)
    assert par["status"] == "ok"


def test_float_noise_below_tolerance_does_not_fail_the_gate():
    """A last-bit difference is not a regression.

    Aggregates are sums of floats, so a run that is arithmetically identical to
    the baseline can still differ by ~1e-16. Without the tolerance the gate
    would fail at random on an unchanged retriever, and a flaky gate gets
    disabled by whoever is on call.
    """
    current = {k: v - GATE_TOLERANCE / 10 for k, v in PERFECT.items()}
    passed, _ = compare_to_baseline(current, dict(PERFECT))
    assert passed


def test_a_drop_just_above_tolerance_does_fail():
    """The other side of the tolerance: it must not swallow a real drop."""
    current = {**PERFECT, "mrr": 1.0 - GATE_TOLERANCE * 100}
    passed, _ = compare_to_baseline(current, dict(PERFECT))
    assert not passed


def test_missing_metric_fails_rather_than_skipping():
    """A gated key absent from the current run is a failure, not a skip.

    If absence meant "nothing to compare", renaming a metric key would silently
    switch the gate off while every build stayed green — the precise failure a
    gate exists to prevent.
    """
    current = {k: v for k, v in PERFECT.items() if k != "mrr"}
    passed, rows = compare_to_baseline(current, dict(PERFECT))
    assert not passed
    assert next(r for r in rows if r["metric"] == "mrr")["status"] == "MISSING"


def test_macro_precision_is_not_gated():
    """Dropping macro precision through the floor must NOT fail the gate.

    It reduces to mean(|expected|)/k, so it moves when the golden set's label
    counts change and stays put when retrieval quality does. Gating on it would
    fail M8 for adding single-answer queries — which the plan asks it to do —
    and would pass anyone who simply labelled more documents per query.
    """
    assert "macro_precision" not in GATE_METRICS
    current = {**PERFECT, "macro_precision": 0.01}
    baseline = {**PERFECT, "macro_precision": 0.99}
    passed, _ = compare_to_baseline(current, baseline)
    assert passed


def test_exit_code_three_is_distinct_from_the_broken_suite_code():
    """3 (regressed) and 2 (broken suite) must not collide.

    They demand opposite responses: a broken suite is fixed by repairing the
    fixture, a regression must never be. One shared non-zero code would let a
    regression be "fixed" by editing the golden set.
    """
    assert EXIT_REGRESSION == 3


def test_gate_rejects_a_file_that_is_not_an_eval_payload(tmp_path):
    bogus = tmp_path / "not_a_baseline.json"
    bogus.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    with pytest.raises(ValueError, match="not an eval result payload"):
        load_baseline(bogus)


def test_gate_reports_a_missing_baseline_file_usefully(tmp_path):
    with pytest.raises(FileNotFoundError, match="run the v1 suite first"):
        load_baseline(tmp_path / "absent.json")
