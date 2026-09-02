"""Unit tests for the pure decay function (M8 step 3).

No database, no network, no clock. `jobs.decay.decay_weight()` is deliberately a
function of its arguments only, and this file is the reason: every property the
decay policy claims can be checked by arithmetic, in milliseconds, with no
fixture to get wrong.

Run:  pytest tests/unit/test_decay.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jobs.decay import (
    age_in_days,
    archive_threshold,
    compute_updates,
    decay_floor,
    decay_weight,
    peak_weight,
)
from jobs.claims import ClaimedRow

pytestmark = pytest.mark.timeout(30)

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# the two tests the plan names
# ---------------------------------------------------------------------------

def test_decay_weight_function_is_monotonic():
    """Weight falls as elapsed time grows, and never drops below the floor.

    Checked as a STRICT decrease over a fine grid before the floor is reached,
    not merely `first > last`. A function that dropped to the floor immediately
    and stayed there would satisfy `first > last` while being a step function
    rather than decay; a function with a bump in the middle would too.
    """
    floor = decay_floor()

    # A dense sweep across the region where decay is still above the floor.
    ages = [d / 4 for d in range(0, 4 * 90 + 1)]  # 0 to 90 days in quarter days
    weights = [decay_weight(age_days=a, reinforcement_count=0) for a in ages]

    above_floor = [(a, w) for a, w in zip(ages, weights) if w > floor]
    assert len(above_floor) > 100, "the test grid never leaves the floor"

    for (age_a, weight_a), (age_b, weight_b) in zip(above_floor, above_floor[1:]):
        assert weight_b < weight_a, (
            f"weight did not decrease between {age_a}d ({weight_a}) and "
            f"{age_b}d ({weight_b}) — decay is not monotonic"
        )

    # The floor holds, including at absurd ages, and is never breached.
    assert min(weights) >= floor
    for age in (365.0, 3650.0, 36500.0, 1e9):
        assert decay_weight(age_days=age, reinforcement_count=0) == pytest.approx(floor)

    # Age zero returns the peak, not something smaller — a memory accessed a
    # moment ago has not decayed at all.
    assert decay_weight(age_days=0.0, reinforcement_count=0) == pytest.approx(
        peak_weight(0)
    )

    # A negative age (clock skew between the app host and the database) must not
    # produce a weight ABOVE the peak.
    assert decay_weight(age_days=-5.0, reinforcement_count=0) <= peak_weight(0)


def test_reinforced_memory_decays_slower():
    """Same age, same starting weight, more reinforcement -> more weight left.

    `base_weight` is pinned EQUAL for both rows on purpose. `peak_weight()`
    already gives a reinforced row a higher starting point, so a test that let
    the peaks differ would pass even if the damping term were deleted entirely —
    it would be measuring "starts higher", not "decays slower". Holding the base
    fixed isolates the half-life damping, which is the claim under test.
    """
    base = 1.0
    age = 45.0  # comfortably past one half-life, well before the floor

    plain = decay_weight(age_days=age, reinforcement_count=0, base_weight=base)
    reinforced = decay_weight(age_days=age, reinforcement_count=6, base_weight=base)

    assert reinforced > plain, (
        f"a memory reinforced 6 times retained {reinforced}, an unreinforced one "
        f"{plain} — the reinforcement damping is not applied"
    )
    assert plain > decay_floor()
    assert reinforced > plain * 1.2, (
        "the damping is present but negligible; six reinforcements should be "
        "clearly visible at 45 days"
    )

    # Monotone in reinforcement count, not just different at one value.
    series = [
        decay_weight(age_days=age, reinforcement_count=n, base_weight=base)
        for n in range(0, 10)
    ]
    for lower, higher in zip(series, series[1:]):
        assert higher > lower, f"weight is not monotone in reinforcement_count: {series}"


# ---------------------------------------------------------------------------
# supporting properties
# ---------------------------------------------------------------------------

def test_decay_is_idempotent_in_its_inputs():
    """Re-running the job cannot double-decay. See jobs/decay.py's header.

    The function never reads the row's *current* weight, so applying it twice to
    the same row on the same night gives the same answer both times. Expressed
    here as: feeding the first result back in as the base does NOT reproduce the
    function, which is exactly the multiplicative bug this design avoids.
    """
    once = decay_weight(age_days=30.0, reinforcement_count=0)
    twice_same_inputs = decay_weight(age_days=30.0, reinforcement_count=0)
    assert once == twice_same_inputs

    multiplicative = decay_weight(age_days=30.0, reinforcement_count=0, base_weight=once)
    assert multiplicative < once, (
        "sanity: a multiplicative second pass WOULD lower the weight further — "
        "which is the failure mode the age-based design exists to avoid"
    )


def test_half_life_is_honoured():
    """At exactly one half-life an un-reinforced memory retains half its peak."""
    from jobs.decay import half_life_days

    hl = half_life_days()
    assert decay_weight(
        age_days=hl, reinforcement_count=0, base_weight=1.0
    ) == pytest.approx(0.5, abs=1e-9)
    assert decay_weight(
        age_days=2 * hl, reinforcement_count=0, base_weight=1.0
    ) == pytest.approx(0.25, abs=1e-9)


def test_peak_weight_tracks_capture_config_and_is_capped():
    """`peak_weight` must agree with M2's writer, and must not run away."""
    from capture import config as capture_config

    increment = capture_config.weight_increment()
    ceiling = capture_config.weight_max()

    assert peak_weight(0) == pytest.approx(1.0)
    assert peak_weight(1) == pytest.approx(min(ceiling, 1.0 + increment))
    assert peak_weight(10_000) == pytest.approx(ceiling)
    assert peak_weight(-3) == pytest.approx(1.0), "a negative count must not shrink the peak"


def test_archive_threshold_sits_above_the_floor():
    """Otherwise nothing can ever fall below it and archiving is unreachable."""
    assert archive_threshold() > decay_floor()


def test_age_in_days_is_floored_and_timezone_safe():
    naive = (NOW - timedelta(days=10)).replace(tzinfo=None)
    assert age_in_days(naive, NOW) == pytest.approx(10.0)
    assert age_in_days(NOW + timedelta(days=5), NOW) == 0.0, "future ages must floor at 0"
    assert age_in_days(NOW - timedelta(hours=12), NOW) == pytest.approx(0.5)


def test_compute_updates_marks_only_rows_below_the_threshold():
    """The archive decision is `new_weight < ARCHIVE_THRESHOLD`, per row.

    Pure — no connection involved — so the boundary can be checked exactly
    rather than inferred from what ended up in the table.
    """
    rows = [
        ClaimedRow("fresh", "s", 1.0, 0, NOW - timedelta(days=1)),
        ClaimedRow("middling", "s", 1.0, 0, NOW - timedelta(days=60)),
        ClaimedRow("ancient", "s", 1.0, 0, NOW - timedelta(days=800)),
        ClaimedRow("ancient-but-loved", "s", 1.0, 50, NOW - timedelta(days=800)),
    ]
    updates = {u.id: u for u in compute_updates(rows, now=NOW)}

    assert updates["fresh"].new_weight > updates["middling"].new_weight
    assert updates["middling"].new_weight > updates["ancient"].new_weight
    assert updates["ancient"].should_archive is True
    assert updates["fresh"].should_archive is False

    # Reinforcement is not a licence to survive forever: 800 days beats even a
    # heavily damped half-life. It should still archive, just later than its
    # unreinforced twin would.
    assert updates["ancient-but-loved"].new_weight >= updates["ancient"].new_weight

    for update in updates.values():
        assert update.should_archive == (update.new_weight < archive_threshold())
