"""Hand-set ranking fixtures (plan step 13).

Every candidate here has its four signals chosen so the weighted score is exact
in binary floating point and computable on paper. That is the whole point: a
test that recomputes the formula to check the formula proves nothing, so the
expected values below are written out as literals and the tests compare against
those literals.

HOW THE SIGNALS ARE SET
-----------------------
The features are read from the candidate, not injected, so each fixture works
backwards from a target signal value to the raw field that produces it:

  semantic    `RetrievalCandidate.score` — already absolute after M3's merge, so
              `semantic_score()` returns it unchanged. Set directly.
  recency     `0.5 ** (age_days / 30)`. Ages are multiples of the 30-day
              half-life, so the values are exactly 1, 1/2, 1/4, 1/8 — all exact
              in binary, no tolerance games.
  frequency   `n / (n + 3)`. n=0 -> 0, n=1 -> 0.25, n=3 -> 0.5, n=9 -> 0.75.
  importance  the `importance` column, used as-is after clamping.

`NOW` is frozen so the ages, and therefore the recency values, never depend on
when the suite runs. Tests must pass `now=NOW` to `rank()` / `score_candidate()`.

REALISM
-------
The contents are the kind of thing M2 actually stores, including the pair of
overlapping cello facts a live turn produced (both rows exist: their cosine
similarity sits below the 0.82 dedup threshold). Ranking must handle
near-duplicate candidates gracefully; it is explicitly not M4's job to
deduplicate them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from retrieve.types import KEYWORD, SEMANTIC, RetrievalCandidate

# Frozen clock. Every age below is measured back from here.
NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

# Mirrors `retrieve.config.RECENCY_HALF_LIFE_DAYS`, restated as a literal so a
# fixture drift shows up as a failing test rather than as silently-rescaled
# expectations.
HALF_LIFE_DAYS = 30.0


def _accessed(age_days: float | None) -> str | None:
    """ISO timestamp `age_days` before `NOW`, matching what the paths store."""
    if age_days is None:
        return None
    return (NOW - timedelta(days=age_days)).isoformat()


def make_candidate(
    memory_id: str,
    content: str,
    *,
    semantic: float,
    age_days: float | None,
    reinforcement_count: int | None,
    importance: float | None,
    path: str = SEMANTIC,
) -> RetrievalCandidate:
    """Build a candidate shaped exactly like one `hybrid_search()` returns."""
    return RetrievalCandidate(
        memory_id=memory_id,
        content=content,
        score=semantic,
        path=path,  # type: ignore[arg-type]
        paths={path},
        path_scores={path: semantic},
        raw_path_scores={path: semantic},
        metadata={
            "source": "chat",
            "importance": importance,
            "confidence": 0.9,
            "weight": 1.0,
            "reinforcement_count": reinforcement_count,
            "created_at": _accessed(age_days),
            "last_accessed_at": _accessed(age_days),
        },
    )


# ---------------------------------------------------------------------------
# the main fixture set — six candidates, six distinct hand-computed scores
# ---------------------------------------------------------------------------
#
#  id       sem   age   rec     n   freq   imp    0.4*sem + 0.2*rec + 0.2*freq + 0.2*imp
#  ------------------------------------------------------------------------------------
#  mem-01   0.90    0   1.000   9   0.75   0.80   0.360 + 0.200 + 0.150 + 0.160 = 0.870
#  mem-02   0.80   30   0.500   3   0.50   0.60   0.320 + 0.100 + 0.100 + 0.120 = 0.640
#  mem-03   0.60   60   0.250   1   0.25   0.50   0.240 + 0.050 + 0.050 + 0.100 = 0.440
#  mem-04   0.50   90   0.125   0   0.00   0.40   0.200 + 0.025 + 0.000 + 0.080 = 0.305
#  mem-05   0.30   90   0.125   0   0.00   0.20   0.120 + 0.025 + 0.000 + 0.040 = 0.185
#  mem-06   0.20   90   0.125   0   0.00   0.10   0.080 + 0.025 + 0.000 + 0.020 = 0.125
#
# Note the ordering is NOT the same as ordering by `semantic` alone would give if
# the other signals were ignored — mem-01 beats mem-02 by more than its semantic
# lead, which is what makes this set able to fail a "sorted by score only"
# implementation.

EXPECTED_SIGNALS: dict[str, dict[str, float]] = {
    "mem-01": {"semantic": 0.90, "recency": 1.000, "frequency": 0.75, "importance": 0.80},
    "mem-02": {"semantic": 0.80, "recency": 0.500, "frequency": 0.50, "importance": 0.60},
    "mem-03": {"semantic": 0.60, "recency": 0.250, "frequency": 0.25, "importance": 0.50},
    "mem-04": {"semantic": 0.50, "recency": 0.125, "frequency": 0.00, "importance": 0.40},
    "mem-05": {"semantic": 0.30, "recency": 0.125, "frequency": 0.00, "importance": 0.20},
    "mem-06": {"semantic": 0.20, "recency": 0.125, "frequency": 0.00, "importance": 0.10},
}

# Hand-computed weighted totals — see the table above.
EXPECTED_SCORES: dict[str, float] = {
    "mem-01": 0.870,
    "mem-02": 0.640,
    "mem-03": 0.440,
    "mem-04": 0.305,
    "mem-05": 0.185,
    "mem-06": 0.125,
}

# The order those scores imply, highest first.
EXPECTED_ORDER: list[str] = ["mem-01", "mem-02", "mem-03", "mem-04", "mem-05", "mem-06"]

CONTENTS: dict[str, str] = {
    "mem-01": "The user plays the cello.",
    "mem-02": "The user plays the cello on Sunday mornings at the community hall.",
    "mem-03": "The user's daughter is named Priya and started school this year.",
    "mem-04": "The user is allergic to shellfish.",
    "mem-05": "The user prefers replies of three sentences or fewer.",
    "mem-06": "The user mentioned liking the smell of rain on hot pavement.",
}


def known_score_candidates() -> list[RetrievalCandidate]:
    """The six fixtures above, deliberately NOT in ranked order.

    Shuffled at construction so a `rank()` that forgot to sort, or a composer
    that drops by list position, cannot pass by accident.
    """
    spec = [
        # (id, semantic, age_days, reinforcement_count, importance, path)
        ("mem-04", 0.50, 90, 0, 0.40, KEYWORD),
        ("mem-01", 0.90, 0, 9, 0.80, SEMANTIC),
        ("mem-06", 0.20, 90, 0, 0.10, KEYWORD),
        ("mem-03", 0.60, 60, 1, 0.50, SEMANTIC),
        ("mem-05", 0.30, 90, 0, 0.20, KEYWORD),
        ("mem-02", 0.80, 30, 3, 0.60, SEMANTIC),
    ]
    return [
        make_candidate(
            memory_id,
            CONTENTS[memory_id],
            semantic=semantic,
            age_days=age,
            reinforcement_count=count,
            importance=importance,
            path=path,
        )
        for memory_id, semantic, age, count, importance, path in spec
    ]


# ---------------------------------------------------------------------------
# tiebreaker fixture
# ---------------------------------------------------------------------------

def tied_candidates() -> list[RetrievalCandidate]:
    """Four candidates with byte-identical signals, supplied out of id order.

    Only `memory_id` can separate them, so the returned order is a direct test
    of the deterministic tiebreaker. Ids are UUID-shaped because that is what
    `memory_id` really holds, and string ordering of UUIDs is the actual
    tiebreak in production.
    """
    ids = [
        "d4f0c0aa-0000-4000-8000-000000000004",
        "a1b2c3d4-0000-4000-8000-000000000001",
        "c3d4e5f6-0000-4000-8000-000000000003",
        "b2c3d4e5-0000-4000-8000-000000000002",
    ]
    return [
        make_candidate(
            memory_id,
            f"The user has a recurring Tuesday commitment ({index}).",
            semantic=0.55,
            age_days=30,
            reinforcement_count=3,
            importance=0.5,
            path=SEMANTIC,
        )
        for index, memory_id in enumerate(ids)
    ]


TIED_EXPECTED_ORDER: list[str] = [
    "a1b2c3d4-0000-4000-8000-000000000001",
    "b2c3d4e5-0000-4000-8000-000000000002",
    "c3d4e5f6-0000-4000-8000-000000000003",
    "d4f0c0aa-0000-4000-8000-000000000004",
]

# 0.4*0.55 + 0.2*0.5 + 0.2*0.5 + 0.2*0.5 = 0.22 + 0.1 + 0.1 + 0.1 = 0.52
TIED_EXPECTED_SCORE = 0.52


# ---------------------------------------------------------------------------
# missing-signal fixture
# ---------------------------------------------------------------------------

def null_signal_candidate() -> RetrievalCandidate:
    """A row with NULL `importance` and no `last_accessed_at` at all.

    Real rows exist like this: `importance` is a nullable column, and a
    candidate assembled outside the retrieval paths carries no timestamp. The
    documented defaults apply — recency 0.0, frequency 0.0, importance 0.5 —
    and the score must still be finite.

        0.4*0.70 + 0.2*0.0 + 0.2*0.0 + 0.2*0.5 = 0.28 + 0 + 0 + 0.1 = 0.38
    """
    return make_candidate(
        "mem-null",
        "The user's landlord is called Mr Okonkwo.",
        semantic=0.70,
        age_days=None,
        reinforcement_count=None,
        importance=None,
        path=SEMANTIC,
    )


NULL_SIGNAL_EXPECTED_SCORE = 0.38


# ---------------------------------------------------------------------------
# composer fixtures
# ---------------------------------------------------------------------------

def oversized_candidates(count: int = 6) -> list[RetrievalCandidate]:
    """`count` candidates of similar length with strictly decreasing scores.

    Scores step down by a clean 0.05 per item so the drop order is obvious on
    inspection, and every memory renders to a similar number of tokens so the
    budget in the tests cuts a predictable number of them.
    """
    sentences = [
        "The user is learning Portuguese and practises with a tutor on Thursday evenings.",
        "The user's partner Dana is vegetarian and dislikes coriander in cooked dishes.",
        "The user commutes by bicycle and keeps a spare inner tube in the hall cupboard.",
        "The user reads science fiction and recently finished a novel about generation ships.",
        "The user's mother lives in Aberdeen and visits for a fortnight every summer.",
        "The user finds long bulleted answers hard to read and prefers short paragraphs.",
        "The user works in the mornings and treats afternoons as meeting time.",
        "The user keeps a sourdough starter and bakes on Saturday mornings without fail.",
    ]
    out: list[RetrievalCandidate] = []
    for index in range(count):
        out.append(
            make_candidate(
                f"big-{index:02d}",
                sentences[index % len(sentences)],
                # 0.95, 0.90, 0.85, ... — distinct, descending, no ties.
                semantic=round(0.95 - 0.05 * index, 4),
                age_days=0,
                reinforcement_count=3,
                importance=0.5,
                path=SEMANTIC,
            )
        )
    return out


def single_oversized_candidate(repeats: int = 200) -> RetrievalCandidate:
    """One memory far larger than any sane budget (plan step 10).

    Not a pathological string of junk — a long, real-shaped recollection, so the
    test exercises the same rendering path a normal memory does.
    """
    body = " ".join(
        f"The user described step {n} of their sourdough method in detail."
        for n in range(1, repeats + 1)
    )
    return make_candidate(
        "mem-huge",
        body,
        semantic=0.99,
        age_days=0,
        reinforcement_count=9,
        importance=0.9,
        path=SEMANTIC,
    )
