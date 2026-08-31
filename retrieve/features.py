"""The four ranking signals (plan steps 1, 2), as pure functions.

Each function takes a `RetrievalCandidate` and returns exactly one float in
`[0, 1]`. Nothing here reads a database, a clock it was not handed, or another
candidate.

TOTAL AND BOUNDED (plan step 2)
-------------------------------
Every function here is **total**: there is no candidate — however malformed,
however many NULL columns it carries — for which it raises or returns `None`.
That is not defensiveness for its own sake. `score_candidate()` sums these four
values, and a single `None` reaching that sum is a `TypeError` on the live chat
path, in a node whose entire job is to be optional. A memory with no
`importance` must cost the user a slightly worse ranking, never a failed reply.

The guarantees, spelled out because the tests assert them:

  * return type is always `float`
  * return value is always in `[0, 1]` — clamped, not merely expected to be
  * `None`, missing keys, wrong types, NaN and negative numbers all resolve to a
    documented default rather than propagating

WHY EACH SIGNAL IS ABSOLUTE, NEVER CORPUS-RELATIVE
--------------------------------------------------
None of these four functions may look at the other candidates. This is the same
property M3 was sent back to fix in `retrieve/hybrid.py:_normalize`: min-max
normalization mapped whichever result happened to be best in a result set to
1.0, so a query whose every candidate was a weak 0.29 similarity produced the
same 1.0 as a query with a genuine 0.85 match. It destroyed the information the
merge needed.

`semantic_score()` in particular MUST NOT re-normalize. The score it reads has
already been put on a fixed absolute scale by M3 — cosine similarity clamped to
[0, 1] for the semantic path, `rank / (rank + TS_RANK_REFERENCE)` for the
keyword path. Rescaling it here against the candidate set would reintroduce
exactly the bug M3 removed, one layer further down and much harder to see.
Clamping is not rescaling: it moves no value that was already in range.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from retrieve import config
from retrieve.types import RetrievalCandidate

__all__ = [
    "semantic_score",
    "recency_score",
    "frequency_score",
    "importance_score",
    "utc_now",
]


# ---------------------------------------------------------------------------
# coercion helpers — the total/bounded guarantee lives here
# ---------------------------------------------------------------------------

def _clamp01(value: float) -> float:
    """Fold any real into [0, 1]. NaN folds to 0.0, not to itself."""
    if math.isnan(value):
        return 0.0
    return max(0.0, min(1.0, value))


def _as_float(value: Any, default: float) -> float:
    """`float(value)`, or `default` for None / non-numeric / NaN / infinity.

    `bool` is rejected explicitly: `True` is an instance of `int` in Python, and
    an `importance` of `True` silently becoming 1.0 would be a lie, not a value.
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(result) or math.isinf(result):
        return default
    return result


def _as_datetime(value: Any) -> datetime | None:
    """Parse a timestamp from a `datetime` or an ISO-8601 string; else `None`.

    Both retrieval paths put `last_accessed_at` into `metadata` as
    `.isoformat()` (see `retrieve/semantic.py:_row_metadata`), but a caller
    building candidates by hand will pass a real `datetime`. Accept both rather
    than making the caller know which.

    A naive datetime is read as UTC. Postgres returns `timestamptz`, so anything
    arriving without a tzinfo lost it in transit rather than genuinely meaning
    local time.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # `fromisoformat` on 3.11 handles the trailing 'Z' form, but be explicit.
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_now() -> datetime:
    """The clock the ranker uses. A seam so tests can pin `now` and stay exact."""
    return datetime.now(timezone.utc)


def _meta(candidate: RetrievalCandidate, key: str) -> Any:
    metadata = getattr(candidate, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    return metadata.get(key)


# ---------------------------------------------------------------------------
# the four signals
# ---------------------------------------------------------------------------

def semantic_score(candidate: RetrievalCandidate) -> float:
    """How well this memory matches *this query*, on M3's absolute scale.

    Reads `candidate.score` — the merged, cross-path-comparable number
    `retrieve/hybrid.py:_merge` produced. See the module docstring: this is a
    clamp, deliberately not a normalization.

    Falls back to the best single-path score if `score` is unusable, and to 0.0
    if the candidate carries no scores at all — a candidate no path scored is
    not a semantic match, and must not be handed a neutral 0.5 that would let it
    outrank a genuinely weak but real match.
    """
    merged = _as_float(getattr(candidate, "score", None), default=float("nan"))
    if not math.isnan(merged):
        return _clamp01(merged)

    path_scores = getattr(candidate, "path_scores", None)
    if isinstance(path_scores, dict) and path_scores:
        usable = [_as_float(v, default=0.0) for v in path_scores.values()]
        return _clamp01(max(usable))
    return 0.0


def recency_score(
    candidate: RetrievalCandidate,
    now: datetime | None = None,
    *,
    half_life_days: float | None = None,
) -> float:
    """Exponential decay on `last_accessed_at`: `0.5 ** (age_days / half_life)`.

    Half-life rather than a linear window, because the shape matches how a fact
    about a person ages: yesterday and the day before are near-identical, while
    "last month" and "last year" are genuinely different claims. Linear decay
    would make the first distinction as large as the second.

    Missing / unparseable `last_accessed_at` → `config.RECENCY_DEFAULT` (0.0).
    A timestamp in the future (clock skew, or a row written mid-request) clamps
    to 1.0 rather than exceeding it.
    """
    accessed = _as_datetime(_meta(candidate, "last_accessed_at"))
    if accessed is None:
        return _clamp01(config.RECENCY_DEFAULT)

    reference = now or utc_now()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    age_days = (reference - accessed).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0

    half_life = half_life_days if half_life_days is not None else config.RECENCY_HALF_LIFE_DAYS
    if half_life <= 0:
        # A non-positive half-life has no meaning; treat every past access as
        # fully decayed rather than raising inside a scoring loop.
        return 0.0

    return _clamp01(0.5 ** (age_days / half_life))


def frequency_score(candidate: RetrievalCandidate) -> float:
    """Saturating transform over `reinforcement_count`: `n / (n + reference)`.

    Saturating, not linear: the jump from 0 to 3 mentions of the same fact says
    much more than the jump from 20 to 23, and a linear map would let one
    obsessively-repeated memory dominate a top-k that should be about relevance.
    With `FREQUENCY_REFERENCE = 3`, three reinforcements score exactly 0.5 and
    the curve approaches but never reaches 1.0.

    Missing / null / negative counts → 0.0. Never reinforced is the honest
    reading of "no count recorded", and 0.0 is also the value a real row with
    `reinforcement_count = 0` gets, so the two agree.
    """
    count = _as_float(_meta(candidate, "reinforcement_count"), default=0.0)
    if count <= 0:
        return 0.0
    reference = config.FREQUENCY_REFERENCE
    if reference <= 0:
        return 1.0
    return _clamp01(count / (count + reference))


def importance_score(candidate: RetrievalCandidate) -> float:
    """M2's `importance` column, clamped to [0, 1].

    `capture/evaluate.py` writes importance already on 0..1, so this is a clamp
    and a null-guard rather than a transform — there is nothing to rescale and,
    per the module docstring, rescaling would be actively harmful.

    NULL `importance` → `config.IMPORTANCE_DEFAULT` (0.5, neutral). See the
    constant's comment: an unscored row is an absence of judgement, not a
    judgement of unimportance.
    """
    return _clamp01(_as_float(_meta(candidate, "importance"), default=config.IMPORTANCE_DEFAULT))
