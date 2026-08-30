"""Tunables for the retrieval read path (plan step 7).

Every value is read from the environment at call time, never frozen at import,
so a test can monkeypatch an env var and see the change without reloading the
module. Defaults live here and nowhere else.
"""

from __future__ import annotations

import os

from store.db import load_env

DEFAULTS = {
    # How many memories M4's ranker keeps after scoring. The merge is a union of
    # two k=5 paths, so up to 10 candidates arrive; 5 is what actually reaches
    # the prompt block.
    "RANKING_TOP_K": "5",
    # Emit the per-candidate score breakdown at INFO while tuning weights
    # (plan step 14). Off by default — one line per candidate per turn is noise
    # in production but exactly what you want when the ranking looks wrong.
    "RANKING_DEBUG": "0",
    # How many rows each path pulls before the merge. Kept small on purpose:
    # the merge is a union, so k=5 per path already yields up to 10 merged
    # candidates — more than M4's ranker will keep — and a generous k would
    # make the semantic path return most of a small corpus, which destroys the
    # distinction the golden set's keyword-only / semantic-only cases exist to
    # measure.
    "SEMANTIC_TOP_K": "5",
    "KEYWORD_TOP_K": "5",
    # Per-path wall-clock budget. The semantic path includes a live embedding
    # API round-trip, so this cannot be as tight as a pure-SQL budget would be.
    "PATH_TIMEOUT_MS": "5000",
}


def _env(name: str) -> str:
    load_env()
    value = os.environ.get(f"RETRIEVE_{name}")
    if value is None or value == "":
        return DEFAULTS[name]
    return value


def ranking_top_k() -> int:
    """How many ranked memories survive into the context block (plan step 4)."""
    return int(_env("RANKING_TOP_K"))


def ranking_debug() -> bool:
    """True when the per-candidate score breakdown should be logged (step 14)."""
    return _env("RANKING_DEBUG").strip().lower() in {"1", "true", "yes", "on"}


def semantic_top_k() -> int:
    return int(_env("SEMANTIC_TOP_K"))


def keyword_top_k() -> int:
    return int(_env("KEYWORD_TOP_K"))


def path_timeout_ms() -> int:
    return int(_env("PATH_TIMEOUT_MS"))


def path_timeout_seconds() -> float:
    return path_timeout_ms() / 1000.0


# Module-level aliases, so `from retrieve.config import SEMANTIC_TOP_K` reads
# naturally in call sites that do not need late binding.
SEMANTIC_TOP_K = semantic_top_k
KEYWORD_TOP_K = keyword_top_k
PATH_TIMEOUT_MS = path_timeout_ms
RANKING_TOP_K = ranking_top_k

# ---------------------------------------------------------------------------
# score normalization (plan step 6)
# ---------------------------------------------------------------------------

# Each path maps its raw score through a FIXED reference scale rather than
# min-max, so a path's scores never depend on what else that path returned.
# The full reasoning — including the measured tie that min-max caused — is in
# `retrieve/hybrid.py:_normalize`.
#
# TS_RANK_REFERENCE is the `ts_rank` PostgreSQL returns for a single-term match
# on a short document, measured against this corpus:
#
#   select ts_rank(to_tsvector('english','The original tiles in the hallway
#                              are cracked beyond repair.'),
#                  websearch_to_tsquery('english','origin'));   -- 0.0607927
#
# Used as the half-saturation point of `rank / (rank + TS_RANK_REFERENCE)`, it
# makes that canonical single-term match score exactly 0.5. Matches on more
# query terms score above it and asymptote toward 1.0; weaker ones fall below.
TS_RANK_REFERENCE = 0.0607927


# ---------------------------------------------------------------------------
# M4 ranking weights (plan steps 3, 5) — THE single definition
# ---------------------------------------------------------------------------
#
# The weighted formula the plan specifies, verbatim:
#
#     score = 0.4 * semantic
#           + 0.2 * recency
#           + 0.2 * frequency
#           + 0.2 * importance
#
# These four constants are the ONLY place those numbers exist. `retrieve/ranking`
# imports them and re-exports them under the same names; no other module may
# spell a weight literal. They are frozen module constants rather than env-tuned
# `_env()` lookups on purpose: a weight set that does not sum to 1.0 silently
# rescales every score, and that is not something a stray environment variable
# should be able to do to a running system. Retuning is a code change and a diff.
#
# Semantic gets double the weight of any other signal because it is the only
# signal about *this query*; recency, frequency and importance are properties of
# the memory that are identical no matter what the user just asked.

WEIGHT_SEMANTIC = 0.4
WEIGHT_RECENCY = 0.2
WEIGHT_FREQUENCY = 0.2
WEIGHT_IMPORTANCE = 0.2

RANKING_WEIGHTS: dict[str, float] = {
    "semantic": WEIGHT_SEMANTIC,
    "recency": WEIGHT_RECENCY,
    "frequency": WEIGHT_FREQUENCY,
    "importance": WEIGHT_IMPORTANCE,
}

# Plan step 5: assert at import time that the weights sum to 1.0.
#
# Written as an explicit raise rather than `assert` because `python -O` strips
# `assert` statements, and this is the one invariant that must hold in every
# interpreter mode: if the weights do not sum to 1, `score_candidate()` no longer
# returns a value in [0, 1] and every downstream threshold is quietly wrong.
_WEIGHT_SUM = sum(RANKING_WEIGHTS.values())
if abs(_WEIGHT_SUM - 1.0) > 1e-9:
    raise RuntimeError(
        "ranking weights must sum to exactly 1.0 so scores stay in [0, 1]; "
        f"got {_WEIGHT_SUM!r} from {RANKING_WEIGHTS!r}"
    )


# ---------------------------------------------------------------------------
# M4 feature-shaping constants (plan steps 1, 2)
# ---------------------------------------------------------------------------
#
# These shape the three non-semantic signals. Each is a *fixed reference*, in the
# same spirit as TS_RANK_REFERENCE above: a signal's value must depend only on
# the memory itself, never on what else happened to be retrieved alongside it.

# Recency: exponential decay on `last_accessed_at`, expressed as a half-life.
# A memory touched today scores 1.0, one untouched for 30 days scores 0.5, one
# untouched for 60 days scores 0.25. 30 days is roughly the horizon over which a
# stated preference stops being a safe assumption about a person.
RECENCY_HALF_LIFE_DAYS = 30.0

# Recency when `last_accessed_at` is missing entirely. The column is NOT NULL in
# 0002_memories.sql, so a missing value means the candidate did not come from a
# `memories` row (a synthetic or hand-built candidate). 0.0 — no evidence of
# access is not evidence of recent access — so a synthetic candidate can never
# out-rank a real one on a signal it has no data for.
RECENCY_DEFAULT = 0.0

# Frequency: saturating transform `n / (n + FREQUENCY_REFERENCE)` over
# `reinforcement_count`. Three reinforcements — the user having said the same
# thing three separate times — scores 0.5; the curve is steep below that and
# flat above, because the difference between 0 and 3 mentions is meaningful and
# the difference between 20 and 30 is not.
FREQUENCY_REFERENCE = 3.0

# Importance when the `importance` column is NULL. 0.5 — neutral. NULL means
# M2's evaluate node never scored the row (an older row, or one written by a
# path that predates scoring); that is an absence of judgement, not a judgement
# of unimportance, so it must neither reward nor punish the memory.
IMPORTANCE_DEFAULT = 0.5
