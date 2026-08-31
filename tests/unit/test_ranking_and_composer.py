"""M4 unit tests — the ranking node and the token-bounded context composer.

Pure functions only: no database, no provider calls, no event loop. The only
external dependency is the tokenizer, and `context/tokens.py` degrades to an
estimate rather than raising if it cannot load one, so this file runs offline.

Every expected number comes from `tests/unit/fixtures/ranking_fixtures.py`,
where it is written as a literal alongside the arithmetic that produced it. No
test recomputes the formula it is checking.
"""

from __future__ import annotations

import math

import pytest

from context import config as context_config
from context import tokens
from context.composer import compose, compose_profile_block, render_block, render_line
from context.tokens import count_tokens, estimate_tokens
from retrieve import config as retrieve_config
from retrieve import features
from retrieve import ranking
from retrieve.ranking import (
    WEIGHT_FREQUENCY,
    WEIGHT_IMPORTANCE,
    WEIGHT_RECENCY,
    WEIGHT_SEMANTIC,
    rank,
    score_breakdown,
    score_candidate,
)
from tests.unit.fixtures.ranking_fixtures import (
    EXPECTED_ORDER,
    EXPECTED_SCORES,
    EXPECTED_SIGNALS,
    NOW,
    NULL_SIGNAL_EXPECTED_SCORE,
    TIED_EXPECTED_ORDER,
    TIED_EXPECTED_SCORE,
    WRONG_WEIGHTINGS,
    known_score_candidates,
    make_candidate,
    null_signal_candidate,
    oversized_candidates,
    single_oversized_candidate,
    tied_candidates,
)

TOLERANCE = 1e-9


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------

def test_ranking_order_matches_weighted_formula():
    """rank() returns the hand-computed weighted order, not the input order."""
    candidates = known_score_candidates()

    # The fixture is deliberately supplied shuffled; if it were pre-sorted this
    # test could pass without any sorting happening at all.
    assert [c.memory_id for c in candidates] != EXPECTED_ORDER

    ranked = rank(candidates, top_k=len(candidates), now=NOW)

    assert [item.memory_id for item in ranked] == EXPECTED_ORDER
    # And the scores that produced that order are the hand-computed ones.
    for item in ranked:
        assert item.score == pytest.approx(EXPECTED_SCORES[item.memory_id], abs=TOLERANCE)


def test_score_matches_hand_computed_value():
    """score_candidate() reproduces 0.4/0.2/0.2/0.2 exactly on a known fixture.

    mem-03's signals are semantic 0.80, recency 0.50, frequency 0.50,
    importance 0.50, so the weighted score must be

        0.4*0.80 + 0.2*0.50 + 0.2*0.50 + 0.2*0.50
      = 0.320    + 0.100    + 0.100    + 0.100     = 0.620
    """
    candidate = next(c for c in known_score_candidates() if c.memory_id == "mem-03")

    breakdown = score_breakdown(candidate, NOW)
    signals = EXPECTED_SIGNALS["mem-03"]

    # Each signal is what the fixture set it to...
    assert breakdown.semantic == pytest.approx(signals["semantic"], abs=TOLERANCE)
    assert breakdown.recency == pytest.approx(signals["recency"], abs=TOLERANCE)
    assert breakdown.frequency == pytest.approx(signals["frequency"], abs=TOLERANCE)
    assert breakdown.importance == pytest.approx(signals["importance"], abs=TOLERANCE)

    # ...and the total is the hand-computed 0.620, spelled out here so a
    # reweighting cannot pass by changing both the code and one constant.
    assert score_candidate(candidate, NOW) == pytest.approx(0.620, abs=TOLERANCE)
    assert breakdown.total == pytest.approx(0.620, abs=TOLERANCE)

    # A wrong weighting that still sums to 1 — 0.25 each — gives
    # 0.25 * (0.80 + 0.50 + 0.50 + 0.50) = 0.25 * 2.30 = 0.575, not 0.620, so
    # this fixture discriminates between weightings on the score as well as on
    # the order.
    assert 0.25 * (0.80 + 0.50 + 0.50 + 0.50) == pytest.approx(0.575, abs=TOLERANCE)
    assert 0.575 != pytest.approx(0.620, abs=1e-3)


@pytest.mark.parametrize("scheme", sorted(WRONG_WEIGHTINGS))
def test_ranking_order_would_break_under_a_wrong_weighting(monkeypatch, scheme):
    """Additional: the ordering fixture actually discriminates between weightings.

    THIS IS THE TEST THAT GIVES `test_ranking_order_matches_weighted_formula`
    ITS MEANING. An ordering assertion proves nothing unless a wrong ranker
    would fail it, and the first version of this fixture had all four signals
    rising together, so every positive weighting produced the identical order —
    the order test would have passed against a ranker that ignored three of its
    four inputs.

    Here each wrong-but-plausible weighting is patched over the real constants
    and the fixture is re-ranked. Every scheme must produce a DIFFERENT order
    from `EXPECTED_ORDER`; if one does not, this fixture has lost its teeth and
    the order test has quietly become decorative again.

    The weights are patched on `retrieve.ranking` rather than on
    `retrieve.config`, because `score_breakdown()` reads its module globals —
    patching the source module would not change the arithmetic.
    """
    semantic, recency, frequency, importance = WRONG_WEIGHTINGS[scheme]
    monkeypatch.setattr(ranking, "WEIGHT_SEMANTIC", semantic)
    monkeypatch.setattr(ranking, "WEIGHT_RECENCY", recency)
    monkeypatch.setattr(ranking, "WEIGHT_FREQUENCY", frequency)
    monkeypatch.setattr(ranking, "WEIGHT_IMPORTANCE", importance)

    candidates = known_score_candidates()
    wrong_order = [item.memory_id for item in rank(candidates, top_k=len(candidates), now=NOW)]

    assert wrong_order != EXPECTED_ORDER, (
        f"the {scheme!r} weighting produced the same order as the correct one; "
        "the fixture's signals must be anti-correlated enough that a wrong "
        "weighting reorders them, or the ordering test proves nothing"
    )


def test_weights_sum_to_one():
    """The declared weights are 0.4/0.2/0.2/0.2 and sum to exactly 1.0."""
    assert WEIGHT_SEMANTIC == 0.4
    assert WEIGHT_RECENCY == 0.2
    assert WEIGHT_FREQUENCY == 0.2
    assert WEIGHT_IMPORTANCE == 0.2

    total = WEIGHT_SEMANTIC + WEIGHT_RECENCY + WEIGHT_FREQUENCY + WEIGHT_IMPORTANCE
    assert total == pytest.approx(1.0, abs=TOLERANCE)

    # Same constants, single definition: ranking re-exports config's values.
    assert WEIGHT_SEMANTIC is retrieve_config.WEIGHT_SEMANTIC
    assert sum(retrieve_config.RANKING_WEIGHTS.values()) == pytest.approx(1.0, abs=TOLERANCE)


def test_top_k_selection():
    """rank() returns exactly RANKING_TOP_K items when more are available."""
    top_k = retrieve_config.ranking_top_k()
    candidates = known_score_candidates()
    assert len(candidates) > top_k, "fixture must over-supply for this to mean anything"

    ranked = rank(candidates, now=NOW)

    assert len(ranked) == top_k
    # The ones kept are the top-scoring ones, not the first k supplied.
    assert [item.memory_id for item in ranked] == EXPECTED_ORDER[:top_k]


def test_ranking_tiebreaker_is_deterministic():
    """Identical-scoring candidates rank in the same order on repeated runs."""
    first = rank(tied_candidates(), top_k=10, now=NOW)
    second = rank(tied_candidates(), top_k=10, now=NOW)

    # The premise: they really do all score the same.
    assert {round(item.score, 12) for item in first} == {round(TIED_EXPECTED_SCORE, 12)}

    assert [item.memory_id for item in first] == [item.memory_id for item in second]
    # ...and the tiebreak is the documented one: memory_id ascending.
    assert [item.memory_id for item in first] == TIED_EXPECTED_ORDER


def test_missing_signal_values_do_not_break_scoring():
    """NULL importance / absent last_accessed_at yield a finite score."""
    candidate = null_signal_candidate()

    # No feature function returns None, and each stays inside [0, 1].
    for value in (
        features.semantic_score(candidate),
        features.recency_score(candidate, NOW),
        features.frequency_score(candidate),
        features.importance_score(candidate),
    ):
        assert isinstance(value, float)
        assert math.isfinite(value)
        assert 0.0 <= value <= 1.0

    score = score_candidate(candidate, NOW)
    assert math.isfinite(score)
    assert 0.0 <= score <= 1.0
    # 0.4*0.70 + 0.2*0.0 (no timestamp) + 0.2*0.0 (no count) + 0.2*0.5 (null
    # importance -> neutral default) = 0.38
    assert score == pytest.approx(NULL_SIGNAL_EXPECTED_SCORE, abs=TOLERANCE)

    # And it survives the whole pipeline, not just the scorer.
    result = compose([candidate], budget=200, now=NOW)
    assert result.memory_ids == ["mem-null"]


# ---------------------------------------------------------------------------
# composer
# ---------------------------------------------------------------------------

def test_composer_respects_token_budget():
    """An over-budget candidate set composes to a block within the budget."""
    budget = 60
    ranked = rank(oversized_candidates(6), top_k=6, now=NOW)

    # Premise: the full set really does not fit.
    assert count_tokens(render_block(ranked)) > budget

    result = compose_profile_block(ranked, budget=budget)

    assert count_tokens(result.block) <= budget
    assert result.token_count == count_tokens(result.block)
    assert result.token_count <= result.budget == budget
    assert result.memory_ids, "budget should have fitted at least one memory"


def test_composer_drops_lowest_ranked_first():
    """The first memory dropped is the lowest-scored one."""
    ranked = rank(oversized_candidates(6), top_k=6, now=NOW)
    lowest = min(ranked, key=lambda item: item.score)

    # A budget that forces exactly one drop.
    result = compose_profile_block(ranked, budget=90)

    assert result.dropped_ids == [lowest.memory_id]
    assert lowest.memory_id not in result.memory_ids
    assert len(result.memory_ids) == len(ranked) - 1


def test_composer_never_drops_higher_while_lower_survives():
    """No dropped memory has a strictly higher score than a surviving one."""
    ranked = rank(oversized_candidates(6), top_k=6, now=NOW)
    scores = {item.memory_id: item.score for item in ranked}

    result = compose_profile_block(ranked, budget=60)

    assert result.dropped_ids, "budget must actually force drops"
    assert result.memory_ids, "and must not drop everything"

    for dropped_id in result.dropped_ids:
        for kept_id in result.memory_ids:
            assert not (scores[kept_id] < scores[dropped_id]), (
                f"{dropped_id} (score {scores[dropped_id]}) was dropped while "
                f"lower-scored {kept_id} (score {scores[kept_id]}) survived"
            )

    # Equivalently: every kept score is >= every dropped score.
    assert min(scores[i] for i in result.memory_ids) >= max(
        scores[i] for i in result.dropped_ids
    )


def test_composer_does_not_truncate_by_position():
    """Every included memory appears complete; nothing is cut mid-sentence."""
    ranked = rank(oversized_candidates(6), top_k=6, now=NOW)
    by_id = {item.memory_id: item for item in ranked}

    result = compose_profile_block(ranked, budget=60)
    assert result.dropped_ids, "this only proves something if the budget bit"

    for memory_id in result.memory_ids:
        item = by_id[memory_id]
        # The whole rendered line, and the whole underlying content, verbatim.
        assert render_line(item) in result.block
        assert item.content in result.block

    # Nothing else leaked in: the block is the header plus exactly one line per
    # included memory, so no fragment of a dropped memory survives anywhere.
    lines = result.block.split(context_config.LINE_SEPARATOR)
    assert lines[0] == context_config.BLOCK_HEADER
    assert len(lines) == len(result.memory_ids) + 1
    for dropped_id in result.dropped_ids:
        assert by_id[dropped_id].content not in result.block


def test_composer_drop_policy_is_score_based_not_positional():
    """Additional: the same memories survive however the list is ordered.

    `test_composer_does_not_truncate_by_position` rules out cutting the string;
    this rules out the other positional shortcut — dropping from the end of the
    list. Handing the composer a reversed (worst-first) list changes every
    position and no score, so a position-based drop keeps the wrong set and a
    score-based drop keeps exactly the same set.
    """
    ranked = rank(oversized_candidates(6), top_k=6, now=NOW)

    in_order = compose_profile_block(ranked, budget=60)
    reversed_order = compose_profile_block(list(reversed(ranked)), budget=60)

    assert in_order.dropped_ids, "budget must actually force drops"
    assert set(reversed_order.memory_ids) == set(in_order.memory_ids)
    assert set(reversed_order.dropped_ids) == set(in_order.dropped_ids)


def test_composer_tie_break_is_by_memory_id_not_list_position():
    """Additional: equal-scoring candidates drop in the same order, any input order.

    `_lowest_scored_index` used to break ties on list position, which matches
    `memory_id` order only when the caller already sorted. `compose()` always
    ranks first so it never saw the difference — but `compose_profile_block()`
    is exported and takes whatever order it is given, and reversing that order
    changed which memories survived.
    """
    ranked = rank(tied_candidates(), top_k=4, now=NOW)
    assert len({round(item.score, 12) for item in ranked}) == 1, "premise: all tied"

    # A budget that fits some but not all of the tied memories.
    budget = count_tokens(render_block(ranked[:2]))

    orderings = {
        "ranked": list(ranked),
        "reversed": list(reversed(ranked)),
        "rotated": ranked[2:] + ranked[:2],
    }
    kept = {
        name: tuple(sorted(compose_profile_block(items, budget=budget).memory_ids))
        for name, items in orderings.items()
    }

    assert len(set(kept.values())) == 1, f"input order changed the surviving set: {kept}"
    # And the set kept is the ids that sort first — the ones rank() prefers.
    assert kept["ranked"] == tuple(sorted(TIED_EXPECTED_ORDER[: len(kept["ranked"])]))


def test_composer_empty_candidates_returns_empty_block():
    """An empty candidate list yields an empty block and raises nothing."""
    for result in (compose([]), compose_profile_block([]), compose(None)):
        assert result.block == ""
        assert result.memory_ids == []
        assert result.dropped_ids == []
        assert result.token_count == 0
        assert result.is_empty


def test_single_oversized_memory_yields_empty_block():
    """A lone memory bigger than the budget yields no block, not a big one."""
    budget = 40
    huge = single_oversized_candidate()
    assert count_tokens(huge.content) > budget, "fixture must exceed the budget"

    result = compose([huge], budget=budget, now=NOW)

    assert result.memory_ids == []
    assert result.dropped_ids == ["mem-huge"]
    assert result.block == ""
    assert result.token_count == 0
    assert count_tokens(result.block) <= budget


# ---------------------------------------------------------------------------
# the tokenizer-free fallback (plan step 6)
# ---------------------------------------------------------------------------

# Scripts and shapes where a chars-per-token divisor breaks down. Each of these
# costs a BPE trained mostly on English far more than one token per character.
NON_ASCII_SAMPLES = [
    "ユーザーはチェロを弾きます。",                      # Japanese
    "🎻🎼🎶 the user practises daily 🎻🎼🎶",             # emoji
    "🇯🇵🇬🇧🇧🇷",                                          # flag emoji (surrogate pairs)
    "המשתמש מנגן בצ'לו",                                # Hebrew
    "L'utilisateur préfère des réponses très concises.",  # accented Latin
    "!!!???...---___===+++***&&&^^^%%%$$$###@@@!!!???",   # punctuation-dense ASCII
    "The user plays the cello on Sunday mornings.",       # plain English control
]


@pytest.mark.parametrize("sample", NON_ASCII_SAMPLES)
def test_token_estimate_never_undercounts_non_ascii(sample):
    """The tokenizer-free fallback is an upper bound, not a guess.

    The original fallback divided character count by 3.0 and under-counted 12 of
    21 samples — Japanese 19 real tokens against 9 estimated, emoji 20 against 7,
    flag emoji 12 against 2. Composed live, that produced a block 60% over its
    budget while believing itself inside it, and the composer's own guard could
    not catch it because the guard re-uses this counter.

    Every sample here would fail that version. The bound now used — UTF-8 byte
    length — holds for any script because a BPE token never decodes to fewer
    than one byte.
    """
    real = count_tokens(sample)  # exact tokenizer for the configured model
    estimated = estimate_tokens(sample)

    assert estimated >= real, (
        f"estimate {estimated} under-counted the real {real} tokens for {sample!r}; "
        "an under-counting estimate emits over-budget blocks"
    )


def test_composer_respects_budget_with_no_tokenizer_available(monkeypatch):
    """The budget guarantee survives the degraded counter, on non-ASCII content.

    Forces the no-tokenizer path and composes Japanese — the case that made the
    old estimate emit a block it believed was 76 tokens and the real tokenizer
    called 163, against a 120-token budget.

    Both halves matter. The sweep's tight budgets are the ones the old estimate
    failed; the loose budget at the end is there so the test cannot pass by
    composing nothing at all.
    """
    japanese = [
        make_candidate(
            f"jp-{index:02d}",
            "ユーザーは日曜日の朝にチェロを弾きます。コミュニティホールで練習します。",
            semantic=0.9 - 0.05 * index,
            age_days=0,
            reinforcement_count=3,
            importance=0.5,
        )
        for index in range(5)
    ]
    ranked = rank(japanese, top_k=5, now=NOW)

    # Bind the real counter before the tokenizer is taken away.
    real_count = count_tokens
    real_full_block = real_count(render_block(ranked))

    monkeypatch.setattr(tokens, "encoding_for", lambda model=None: None)
    results = {
        budget: compose_profile_block(ranked, budget=budget) for budget in (120, 160, 200, 400)
    }
    monkeypatch.undo()

    for budget, result in results.items():
        # The degraded counter's own view is within budget...
        assert result.token_count <= budget
        # ...and so is the truth, which is the property that actually matters.
        assert real_count(result.block) <= budget, (
            f"block was {real_count(result.block)} real tokens against a "
            f"{budget}-token budget; the fallback counter under-counted"
        )

    # Non-vacuity: the tight budgets are meaningful (the whole set really does
    # not fit at 120), and the loose one still composes something.
    assert real_full_block > 120
    assert results[400].memory_ids


# ---------------------------------------------------------------------------
# additional coverage for plan steps 9 and 12 (not in the plan's test list)
# ---------------------------------------------------------------------------

def test_block_overhead_counts_inside_the_budget():
    """Plan step 9: the header's tokens come out of the budget, not on top."""
    ranked = rank(oversized_candidates(3), top_k=3, now=NOW)

    line_tokens = sum(count_tokens(render_line(item)) for item in ranked)
    block_tokens = count_tokens(render_block(ranked))
    overhead = block_tokens - line_tokens
    assert overhead > 0, "the header and separators must cost something"

    # A budget that fits every line but not the lines plus the header. If the
    # overhead were added on top of the budget instead of taken out of it, all
    # three would survive.
    budget = block_tokens - 1
    result = compose_profile_block(ranked, budget=budget)

    assert result.token_count <= budget
    assert len(result.memory_ids) < len(ranked)


def test_compose_returns_included_memory_ids_in_rank_order():
    """Plan step 12: compose() returns the ids M7 will audit, in block order."""
    result = compose(known_score_candidates(), budget=400, top_k=6, now=NOW)

    assert result.memory_ids == EXPECTED_ORDER
    assert result.block.startswith(context_config.BLOCK_HEADER)
    # The ids line up positionally with the rendered lines.
    lines = result.block.split(context_config.LINE_SEPARATOR)[1:]
    assert len(lines) == len(result.memory_ids)
