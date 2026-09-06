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
    accessed_days_ago: float | None = None,
) -> RetrievalCandidate:
    """Build a candidate shaped exactly like one `hybrid_search()` returns.

    `age_days` sets `created_at` — when the fact was STATED, which drives
    `recency_score`. `accessed_days_ago` sets `last_accessed_at` — when it was
    last READ, which drives `activation_score`. Passing only `age_days` makes
    them equal, which is how the six main candidates below are built.

    THEY MUST BE SETTABLE SEPARATELY, and the reason is the defect M9 fixed.
    While these two were always equal here, the fixture could not distinguish
    the two signals at all: re-scoring the main set with the recency and
    activation weights SWAPPED produces a byte-identical order. Any test built
    only on that set would pass against a ranker that had them backwards — which
    is the precise bug that shipped. `supersession_candidates()` below is the
    set that can tell them apart.
    """
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
            "last_accessed_at": _accessed(
                age_days if accessed_days_ago is None else accessed_days_ago
            ),
        },
    )


# ---------------------------------------------------------------------------
# the main fixture set — six candidates, six distinct hand-computed scores
# ---------------------------------------------------------------------------
#
# These six set `created_at == last_accessed_at`, so recency == activation and
# the two terms collapse to a single 0.35 coefficient on that shared value:
#
#     0.35*sem + 0.25*rec + 0.10*act + 0.10*freq + 0.20*imp
#
#  id       sem   age   rec=act   n   freq   imp    weighted total
#  ------------------------------------------------------------------------------------
#  mem-01   0.95   90   0.125     0   0.00   0.10   0.3325+0.03125+0.0125+0.000+0.020 = 0.39625
#  mem-02   0.60    0   1.000     9   0.75   0.80   0.2100+0.25000+0.1000+0.075+0.160 = 0.79500
#  mem-03   0.80   30   0.500     3   0.50   0.50   0.2800+0.12500+0.0500+0.050+0.100 = 0.60500
#  mem-04   0.70   60   0.250     1   0.25   0.30   0.2450+0.06250+0.0250+0.025+0.060 = 0.41750
#  mem-05   0.35   30   0.500     9   0.75   0.90   0.1225+0.12500+0.0500+0.075+0.180 = 0.55250
#  mem-06   0.25   90   0.125     0   0.00   0.20   0.0875+0.03125+0.0125+0.000+0.040 = 0.17125
#
# The ORDER is unchanged from M4's weights — mem-01 is still the trap that a
# semantic-only ranker puts first and the correct weighting puts fifth — so this
# set keeps every discriminating property described below. Only the totals moved.
#
# THE SIGNALS ARE DELIBERATELY ANTI-CORRELATED. READ THIS BEFORE EDITING THEM.
# ---------------------------------------------------------------------------
# The first version of this fixture had all four signals rise together —
# the best memory was best at everything, the worst worst at everything. Every
# hand-set number was correct and the resulting order was genuinely the weighted
# order, so it looked fine. It was worthless.
#
# When all four signals are co-monotonic, EVERY set of positive weights produces
# the SAME order. The verifier re-ranked that fixture under five schemes — the
# correct 0.4/0.2/0.2/0.2, equal 0.25 each, semantic-only 1/0/0/0, an inverted
# 0.2/0.2/0.2/0.4 and a recency-heavy 0.1/0.7/0.1/0.1 — and all five agreed.
# `test_ranking_order_matches_weighted_formula` would have passed against a
# ranker that ignored three of its four inputs.
#
# This set is built so the schemes DISAGREE, which is the only way an ordering
# test can testify to anything:
#
#   mem-01  the trap. Best semantic match in the set (0.95) and worst at
#           everything else — stale, never reinforced, judged unimportant. A
#           semantic-only ranker puts it FIRST; the correct weighting puts it
#           FIFTH. One candidate does most of the discriminating work.
#   mem-05  the mirror image. Weak semantic match (0.35), but fresh, heavily
#           reinforced and the most important thing in the set. Importance- and
#           recency-heavy schemes lift it above mem-03; the correct weighting
#           does not.
#
# `WRONG_WEIGHTINGS` below records the four schemes, and
# `test_ranking_order_would_break_under_a_wrong_weighting` asserts each one
# actually reorders this set. If you change a signal here, that test is what
# tells you whether you destroyed the fixture's discriminating power.

EXPECTED_SIGNALS: dict[str, dict[str, float]] = {
    "mem-01": {"semantic": 0.95, "recency": 0.125, "frequency": 0.00, "importance": 0.10},
    "mem-02": {"semantic": 0.60, "recency": 1.000, "frequency": 0.75, "importance": 0.80},
    "mem-03": {"semantic": 0.80, "recency": 0.500, "frequency": 0.50, "importance": 0.50},
    "mem-04": {"semantic": 0.70, "recency": 0.250, "frequency": 0.25, "importance": 0.30},
    "mem-05": {"semantic": 0.35, "recency": 0.500, "frequency": 0.75, "importance": 0.90},
    "mem-06": {"semantic": 0.25, "recency": 0.125, "frequency": 0.00, "importance": 0.20},
}

# Hand-computed weighted totals — see the table above.
EXPECTED_SCORES: dict[str, float] = {
    "mem-01": 0.39625,
    "mem-02": 0.79500,
    "mem-03": 0.60500,
    "mem-04": 0.41750,
    "mem-05": 0.55250,
    "mem-06": 0.17125,
}

# The order those scores imply, highest first. Note it is NOT the order of the
# ids, NOT the order of `semantic` (which would start mem-01), and NOT the order
# of `importance` (which would start mem-05).
EXPECTED_ORDER: list[str] = ["mem-02", "mem-03", "mem-05", "mem-04", "mem-01", "mem-06"]

# Weightings that are wrong but plausible — each is something a mis-implemented
# ranker would actually do. Every one of these must reorder the set above.
WRONG_WEIGHTINGS: dict[str, tuple[float, float, float, float, float]] = {
    # (semantic, recency, activation, frequency, importance)
    "equal": (0.2, 0.2, 0.2, 0.2, 0.2),
    "semantic_only": (1.0, 0.0, 0.0, 0.0, 0.0),
    "importance_heavy": (0.2, 0.15, 0.05, 0.1, 0.5),
    "recency_heavy": (0.1, 0.5, 0.2, 0.1, 0.1),
}

# WHAT IS NOT IN THE DICT ABOVE, AND WHY — worth reading before adding to it.
#
# The obvious extra scheme is "swap recency and activation", i.e. weight
# "recently read" over "recently stated", which sounds like the M4 behaviour the
# split removed. It was tried, in both fixture sets, and it discriminates in
# NEITHER:
#
#   * on the six candidates above, created_at == last_accessed_at, so the two
#     signals are numerically identical and swapping their weights is a no-op —
#     the order comes out byte-identical;
#   * on `supersession_candidates()` below, all three share one
#     `last_accessed_at`, so activation is CONSTANT across the set and
#     contributes nothing to the ordering whatever weight it carries. Recency
#     still decides, just more weakly, and the order stays correct.
#
# The mutation that actually reproduces the bug is zeroing the creation-time
# weight — ignoring `created_at` entirely, which is what M4 did by never reading
# the column. That is `test_m4_weighting_tied_all_three_preferences`, and it
# fails loudly if the split is undone.
#
# Recorded rather than deleted, because "we tried the obvious mutation and it
# proved nothing" is the kind of finding that otherwise gets rediscovered.

CONTENTS: dict[str, str] = {
    # The best lexical match in the set, and nearly worthless: a one-off
    # question from months ago that the user never returned to.
    "mem-01": "The user once asked which rosin suits a cello bow in dry weather.",
    # The near-duplicate pair M2's extractor really produced from one turn —
    # both rows exist, their similarity is below the 0.82 dedup threshold.
    "mem-02": "The user plays the cello.",
    "mem-03": "The user plays the cello on Sunday mornings at the community hall.",
    "mem-04": "The user's cello case has a broken latch.",
    # Weak match for a cello query, but the single most important fact here.
    "mem-05": "The user is severely allergic to shellfish.",
    "mem-06": "The user mentioned liking the smell of rain on hot pavement.",
}


def known_score_candidates() -> list[RetrievalCandidate]:
    """The six fixtures above, deliberately NOT in ranked order.

    Shuffled at construction so a `rank()` that forgot to sort, or a composer
    that drops by list position, cannot pass by accident.
    """
    spec = [
        # (id, semantic, age_days, reinforcement_count, importance, path)
        ("mem-04", 0.70, 60, 1, 0.30, KEYWORD),
        ("mem-01", 0.95, 90, 0, 0.10, SEMANTIC),
        ("mem-06", 0.25, 90, 0, 0.20, KEYWORD),
        ("mem-03", 0.80, 30, 3, 0.50, SEMANTIC),
        ("mem-05", 0.35, 30, 9, 0.90, KEYWORD),
        ("mem-02", 0.60, 0, 9, 0.80, SEMANTIC),
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

# 0.35*0.55(sem) + 0.25*0.5(rec) + 0.10*0.5(act) + 0.10*0.5(freq) + 0.20*0.5(imp)
#   = 0.1925 + 0.125 + 0.05 + 0.05 + 0.10 = 0.5175
# (age_days=30 with no separate access time, so recency == activation == 0.5)
TIED_EXPECTED_SCORE = 0.5175


# ---------------------------------------------------------------------------
# missing-signal fixture
# ---------------------------------------------------------------------------

def null_signal_candidate() -> RetrievalCandidate:
    """A row with NULL `importance` and no `last_accessed_at` at all.

    Real rows exist like this: `importance` is a nullable column, and a
    candidate assembled outside the retrieval paths carries no timestamp. The
    documented defaults apply — recency 0.0, activation 0.0, frequency 0.0,
    importance 0.5 — and the score must still be finite.

    Note that BOTH time-based signals default to 0.0 here, and for the same
    reason: no evidence of when a fact was stated is not evidence that it was
    stated recently, and no evidence of access is not evidence of recent access.
    A synthetic candidate must never out-rank a real one on a signal it has no
    data for.

        0.35*0.70(sem) + 0.25*0.0(rec) + 0.10*0.0(act) + 0.10*0.0(freq)
          + 0.20*0.5(imp) = 0.245 + 0 + 0 + 0 + 0.10 = 0.345
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


NULL_SIGNAL_EXPECTED_SCORE = 0.345


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


# ---------------------------------------------------------------------------
# supersession — the set that can tell recency from activation
# ---------------------------------------------------------------------------
#
# THIS IS A REGRESSION FIXTURE FOR A BUG THAT SHIPPED, reproduced from real
# data. A user stated three programming-language preferences over two days —
# Python, then Java, then C++ — and the assistant kept answering in Python. The
# live rows were identical on every signal the ranker had:
#
#   content                              importance  weight  reinforced  last_accessed
#   "…likes code examples in Python"           0.70    1.50           2   09-05 11:31
#   "The user prefers Java."                   0.70    1.50           2   09-05 11:31
#   "The user prefers c++."                    0.70    1.50           2   09-05 11:31
#
# `last_accessed_at` was identical because RETRIEVAL WRITES IT on every row it
# returns. M4's recency term decayed on that column, so all three scored the
# same on the one signal that should have separated them, and the tie fell
# through to semantic similarity between three near-identical sentences.
#
# These three model exactly that: same semantic, same reinforcement, same
# importance, ALL READ AT THE SAME MOMENT (accessed_days_ago=0, as retrieval
# leaves them), differing only in when they were STATED.
#
#   id              stated   rec     accessed   act    weighted total
#   ------------------------------------------------------------------------
#   pref-a-python   60d      0.25    0d         1.0    0.525 + 0.0625 = 0.5875
#   pref-b-java     30d      0.50    0d         1.0    0.525 + 0.1250 = 0.6500
#   pref-c-cpp       0d      1.00    0d         1.0    0.525 + 0.2500 = 0.7750
#
#   where 0.525 = 0.35*0.70(sem) + 0.10*1.0(act) + 0.10*0.4(freq) + 0.20*0.70(imp)
#
# Correct order is newest-stated first: c-cpp, b-java, a-python.
#
# THE IDS ARE ALPHABETICAL BY AGE ON PURPOSE. Under M4's formula all three score
# exactly 0.70 — a three-way tie — and `rank()` breaks ties on `memory_id`
# ascending, which yields a-python, b-java, c-cpp: precisely the wrong answer,
# oldest first, which is the behaviour the user reported. So this fixture does
# not merely fail under the old weighting, it reproduces the observed symptom.

SUPERSESSION_SEMANTIC = 0.70
SUPERSESSION_REINFORCEMENT = 2  # -> frequency 2/(2+3) = 0.4
SUPERSESSION_IMPORTANCE = 0.70

#: Correct order under the M9 weights: most recently STATED first.
SUPERSESSION_EXPECTED_ORDER: list[str] = ["pref-c-cpp", "pref-b-java", "pref-a-python"]

#: Hand-computed M9 totals — see the table above.
SUPERSESSION_EXPECTED_SCORES: dict[str, float] = {
    "pref-a-python": 0.5875,
    "pref-b-java": 0.6500,
    "pref-c-cpp": 0.7750,
}

#: What M4's formula gave every one of them: a three-way tie.
#:
#:     0.4*0.70(sem) + 0.2*1.0(rec-on-ACCESS) + 0.2*0.4(freq) + 0.2*0.70(imp)
#:   = 0.28 + 0.20 + 0.08 + 0.14 = 0.70
SUPERSESSION_M4_TIED_SCORE = 0.70

SUPERSESSION_CONTENTS: dict[str, str] = {
    "pref-a-python": "The user prefers to receive code in Python.",
    "pref-b-java": "The user prefers Java.",
    "pref-c-cpp": "The user prefers c++.",
}

#: How long ago each was STATED. All three are read at the same instant.
SUPERSESSION_STATED_DAYS_AGO: dict[str, float] = {
    "pref-a-python": 60.0,
    "pref-b-java": 30.0,
    "pref-c-cpp": 0.0,
}


def supersession_candidates() -> list[RetrievalCandidate]:
    """Three contradictory preferences, read together, stated at different times.

    Deliberately returned OLDEST FIRST, so a ranker that does nothing at all
    would return the wrong order and the ordering test cannot pass by accident.
    """
    return [
        make_candidate(
            memory_id,
            SUPERSESSION_CONTENTS[memory_id],
            semantic=SUPERSESSION_SEMANTIC,
            age_days=SUPERSESSION_STATED_DAYS_AGO[memory_id],
            # Every one of them was just retrieved — this is the whole point.
            accessed_days_ago=0.0,
            reinforcement_count=SUPERSESSION_REINFORCEMENT,
            importance=SUPERSESSION_IMPORTANCE,
        )
        for memory_id in ("pref-a-python", "pref-b-java", "pref-c-cpp")
    ]
