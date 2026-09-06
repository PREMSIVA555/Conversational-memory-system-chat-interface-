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
    # --- M5 circuit breaker (plan step 6) ---------------------------------
    # See the block comment further down for why each number is what it is.
    "BREAKER_FAILURE_THRESHOLD": "3",
    "BREAKER_COOLDOWN_SECONDS": "30",
    "RETRIEVAL_TIMEOUT_MS": "6000",
    "BREAKER_REDIS_KEY": "memsys:breaker:retrieval",
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


# ---------------------------------------------------------------------------
# M5 circuit breaker (plan step 6)
# ---------------------------------------------------------------------------
#
# BREAKER_FAILURE_THRESHOLD = 3
#   Consecutive failures that take the circuit from `closed` to `open`. One
#   failure is noise — a single dropped socket, one slow embedding call. Three
#   in a row, with no success between them, is a pattern. Higher would make the
#   breaker slow to protect the reply path; lower would trip on transients and
#   blind the assistant to its own memory for no reason.
#
# BREAKER_COOLDOWN_SECONDS = 30
#   How long `open` lasts before one probe is allowed through (`half_open`).
#   Long enough that a restarting Postgres or a rate-limit window has a real
#   chance to clear, short enough that a user in a live conversation gets their
#   memory back within a turn or two rather than after the session is over.
#
# RETRIEVAL_TIMEOUT_MS = 6000
#   The breaker's own wall-clock budget for one whole retrieval call. It sits
#   OUTSIDE `hybrid_search`, which already gives each path PATH_TIMEOUT_MS
#   (5000) individually. The two paths run concurrently, so a fully-timed-out
#   hybrid search returns at ~5000ms; 6000 leaves headroom for the merge and
#   for pool checkout without ever firing before the inner timeouts have had
#   their chance. Set below PATH_TIMEOUT_MS and this timeout would pre-empt the
#   per-path isolation M3 built, converting a half-degraded (still useful)
#   result into a total failure and tripping the breaker on it.
#
# BREAKER_REDIS_KEY = "memsys:breaker:retrieval"
#   ONE namespaced key holding the entire state record as a JSON string, so
#   every replica reads and writes the same record and a human can inspect it
#   with a plain `redis-cli GET`. The single-flight probe lock lives at
#   `<key>:probe` — see `retrieve/breaker.py`.

def breaker_failure_threshold() -> int:
    """Consecutive failures that open the circuit."""
    return max(1, int(_env("BREAKER_FAILURE_THRESHOLD")))


def breaker_cooldown_seconds() -> float:
    """Seconds an open circuit waits before allowing one half-open probe."""
    return float(_env("BREAKER_COOLDOWN_SECONDS"))


def retrieval_timeout_ms() -> int:
    """Wall-clock budget for one guarded retrieval call."""
    return int(_env("RETRIEVAL_TIMEOUT_MS"))


def retrieval_timeout_seconds() -> float:
    return retrieval_timeout_ms() / 1000.0


def breaker_redis_key() -> str:
    """The single key holding the shared breaker state record."""
    return _env("BREAKER_REDIS_KEY")


# Module-level aliases, so `from retrieve.config import SEMANTIC_TOP_K` reads
# naturally in call sites that do not need late binding.
SEMANTIC_TOP_K = semantic_top_k
KEYWORD_TOP_K = keyword_top_k
PATH_TIMEOUT_MS = path_timeout_ms
RANKING_TOP_K = ranking_top_k
BREAKER_FAILURE_THRESHOLD = breaker_failure_threshold
BREAKER_COOLDOWN_SECONDS = breaker_cooldown_seconds
RETRIEVAL_TIMEOUT_MS = retrieval_timeout_ms
BREAKER_REDIS_KEY = breaker_redis_key

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
# M4 specified four terms:
#
#     score = 0.4 * semantic + 0.2 * recency + 0.2 * frequency + 0.2 * importance
#
# where `recency` decayed on `last_accessed_at`. M9 SPLITS THAT TERM IN TWO, and
# the reason is a measured failure rather than a preference.
#
# THE BUG THIS FIXES
# ------------------
# A user stated three programming-language preferences over two days — Python,
# then Java, then C++ — and the assistant kept answering in the OLDEST one. The
# store held all three as live, mutually contradictory rows, and every ranking
# signal was IDENTICAL across them:
#
#     content                         importance  weight  reinforced  last_accessed
#     "…likes code examples in Python"      0.70    1.50           2   09-05 11:31
#     "The user prefers Java."              0.70    1.50           2   09-05 11:31
#     "The user prefers c++."               0.70    1.50           2   09-05 11:31
#
# `last_accessed_at` was identical because REINFORCEMENT stamps it. Every one of
# those rows had been said more than once (reinforcement_count = 2), and
# `store/memories.py:reinforce()` sets `last_accessed_at = now()` — it is the
# ONLY writer of that column in the codebase.
#
# CORRECTION, recorded rather than quietly edited. The first version of this
# comment said retrieval writes the column on every row it returns. It does not:
# a cold verifier ran the real `keyword_search` and `semantic_search` and the
# timestamp did not move, and neither path contains an UPDATE at all. The fix
# below is unaffected — the two signals still needed separating — but the
# mechanism is reinforcement, not reading.
#
# That makes the provenance rule in `store/memories.py` (an assistant_note may
# not reinforce) MORE load-bearing than it first appeared: the assistant
# restating a fact was refreshing this timestamp, which is precisely how three
# contradictory preferences came to look equally fresh.
#
# Reading a memory is not evidence that the fact is fresh. It is evidence that
# the fact is USEFUL. Those are different claims and they now have different
# terms:
#
#     score = 0.35 * semantic          how well it matches THIS query
#           + 0.25 * recency           decay on created_at   — when it was STATED
#           + 0.10 * activation        decay on last_accessed_at — when last READ
#           + 0.10 * frequency         saturating reinforcement_count
#           + 0.20 * importance        M2's evaluate node
#
# WHY THESE NUMBERS
# -----------------
# `recency` is deliberately 2.5x `activation`. That ordering is the whole point:
# a fact stated more recently must be able to outrank one merely read more
# recently, or the bug above returns. Anything that lets activation approach
# recency re-creates it.
#
# `frequency` drops from 0.2 to 0.1 because it turned out to be an amplifier for
# a feedback loop: capture reads the assistant's reply as well as the user's
# message, so the assistant restating a preference reinforced it, which raised
# its frequency score, which made it more likely to be retrieved and restated.
# Repetition was outcompeting recency. M9 also stops `assistant_note` rows from
# incrementing the counter (see `capture/`), but the weight comes down too.
#
# `semantic` drops 0.4 -> 0.35 to fund the split while keeping it the largest
# single term — it is still the only signal about the query the user just typed.
#
# These constants are the ONLY place the numbers exist. `retrieve/ranking`
# imports and re-exports them; no other module may spell a weight literal. They
# are frozen module constants rather than `_env()` lookups on purpose: a weight
# set that does not sum to 1.0 silently rescales every score, and a stray
# environment variable should not be able to do that to a running system.
# Retuning is a code change and a diff.

WEIGHT_SEMANTIC = 0.35
WEIGHT_RECENCY = 0.25
WEIGHT_ACTIVATION = 0.10
WEIGHT_FREQUENCY = 0.10
WEIGHT_IMPORTANCE = 0.20

RANKING_WEIGHTS: dict[str, float] = {
    "semantic": WEIGHT_SEMANTIC,
    "recency": WEIGHT_RECENCY,
    "activation": WEIGHT_ACTIVATION,
    "frequency": WEIGHT_FREQUENCY,
    "importance": WEIGHT_IMPORTANCE,
}

# The invariant that makes the split safe, asserted rather than assumed: a fact
# stated recently must be able to beat one merely read recently.
if WEIGHT_RECENCY <= WEIGHT_ACTIVATION:
    raise RuntimeError(
        "WEIGHT_RECENCY must exceed WEIGHT_ACTIVATION, or read access mimics "
        "temporal freshness again and a superseded preference can outrank the "
        f"one that replaced it; got recency={WEIGHT_RECENCY!r} "
        f"activation={WEIGHT_ACTIVATION!r}"
    )

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

# Recency: exponential decay on `created_at` — WHEN THE FACT WAS STATED.
#
# Not `last_accessed_at`. That was the M4 behaviour and it is the bug documented
# above the weights: retrieval writes `last_accessed_at` on every row it returns,
# so the column measures how recently a memory was USED, and using it as recency
# made three contradictory preferences indistinguishable.
#
# A memory stated today scores 1.0, one stated 30 days ago 0.5, 60 days ago 0.25.
# 30 days is roughly the horizon over which a stated preference stops being a
# safe assumption about a person.
RECENCY_HALF_LIFE_DAYS = 30.0

# Recency when `created_at` is missing. The column is NOT NULL in
# 0002_memories.sql, so a missing value means the candidate did not come from a
# `memories` row (a synthetic or hand-built one). 0.0 — no evidence of when a
# claim was made is not evidence that it was made recently — so a synthetic
# candidate can never out-rank a real one on a signal it has no data for.
RECENCY_DEFAULT = 0.0

# Activation: exponential decay on `last_accessed_at` — when it was last READ.
#
# This is the signal M4 called "recency", kept because it is genuinely useful:
# a memory the system keeps reaching for is probably relevant to how this person
# uses the assistant. It is simply not evidence about whether the fact is
# CURRENT, which is what the split exists to separate.
#
# THE SAME half-life as recency, and that is a deliberate non-choice.
#
# A shorter one is tempting — usefulness plausibly moves faster than truth — but
# the separation this split exists to create comes from the two signals reading
# DIFFERENT TIMESTAMPS and carrying DIFFERENT WEIGHTS, not from different decay
# shapes. Adding a third difference buys nothing measured and costs something
# real: `tests/unit/fixtures/ranking_fixtures.py` chooses ages that are
# multiples of the half-life so every expected score is exact in binary and
# computable on paper. Two half-lives means ages that are multiples of both, or
# expectations nobody can check by hand.
#
# Retune it when there is evidence about how access recency actually behaves in
# this system, not before.
ACTIVATION_HALF_LIFE_DAYS = RECENCY_HALF_LIFE_DAYS

# Activation when `last_accessed_at` is missing: same reasoning as
# RECENCY_DEFAULT. No evidence of access is not evidence of recent access.
ACTIVATION_DEFAULT = 0.0

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
