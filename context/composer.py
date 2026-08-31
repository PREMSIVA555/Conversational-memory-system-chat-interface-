"""The token-bounded context composer (plan steps 7-10, 12).

    ranked candidates ─> render block ─> over budget? ─> drop LOWEST SCORE ─┐
                              ^                                             │
                              └─────────────────────────────────────────────┘

`compose()` is the single entry point M5's response graph calls. It returns a
`ComposedContext` carrying both the rendered block and the ids of the memories
actually included — M7 needs those ids to write an audit record of what the
model was shown, which cannot be reconstructed from the string after the fact.


THE DROP POLICY (plan step 8) — READ THIS BEFORE CHANGING `_fit`
-----------------------------------------------------------------
When the block does not fit, the composer removes **the memory with the lowest
ranking score**, then re-renders and re-counts. It does not:

  * truncate the rendered string to N tokens — that would cut a memory in half
    and hand the model a sentence that says something the user never said. A
    half-rendered "The user is allergic to" is worse than no memory at all.
  * drop from the end of the list — which is *usually* the lowest-scored item
    and therefore usually right, which is exactly what makes it a dangerous
    shortcut. It couples correctness to an ordering invariant maintained
    somewhere else, and it stops being right the moment a caller passes
    candidates in any order but rank order.
  * pop a fixed number of items — the loop re-measures after each removal
    because token counts are not additive across a join.

`_fit` therefore selects its victim with `min()` over the scores. The resulting
invariant, asserted by `test_composer_never_drops_higher_while_lower_survives`:
no dropped memory has a strictly higher score than any surviving one.

BUDGET ACCOUNTING (plan step 9)
-------------------------------
The header and the newlines between lines are counted inside the budget, not on
top of it, because the counter is applied to the finished block string. There is
no separate "overhead" term to keep in sync — the thing measured is the thing
sent.

THE DEGENERATE CASE (plan step 10)
----------------------------------
A single memory longer than the entire budget cannot be included in any form.
The loop drops it like any other over-budget selection, the selection empties,
and the result is an empty block. The composer never emits an over-budget block,
and never emits a partial memory to make one fit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

from context import config
from context.tokens import count_tokens
from retrieve.ranking import RankedCandidate, rank
from retrieve.types import RetrievalCandidate

logger = logging.getLogger(__name__)

__all__ = ["ComposedContext", "compose", "compose_profile_block", "render_block", "render_line"]


@dataclass(frozen=True, slots=True)
class ComposedContext:
    """What the composer hands back (plan step 12).

    block        the rendered text to prepend to the prompt. `""` when nothing
                 was included — an empty block, never a lone header with no
                 memories under it.
    memory_ids   ids of the memories actually in `block`, in the order they
                 appear. This is M7's audit trail: what the model was shown.
    dropped_ids  ids that were ranked but did not fit, lowest score first — the
                 order they were dropped in.
    token_count  measured tokens of `block`; the invariant is `<= budget`.
    """

    block: str = ""
    memory_ids: list[str] = field(default_factory=list)
    dropped_ids: list[str] = field(default_factory=list)
    token_count: int = 0
    budget: int = 0
    included: list[RankedCandidate] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.memory_ids

    @property
    def dropped_for_budget(self) -> bool:
        """True when at least one ranked memory was cut to fit the budget."""
        return bool(self.dropped_ids)

    def to_dict(self) -> dict:
        return {
            "block": self.block,
            "memory_ids": list(self.memory_ids),
            "dropped_ids": list(self.dropped_ids),
            "token_count": self.token_count,
            "budget": self.budget,
        }


# ---------------------------------------------------------------------------
# rendering (plan step 7)
# ---------------------------------------------------------------------------

def _clean(content: str) -> str:
    """Flatten a memory onto one line.

    A memory containing a newline would otherwise break out of its bullet and
    read to the model as a new instruction rather than as a remembered fact —
    the same class of problem as prompt injection, arriving through a stored
    row instead of a live message.
    """
    return " ".join((content or "").split())


def render_line(item: RankedCandidate | RetrievalCandidate) -> str:
    """One memory's rendered line, exactly as it appears in the block.

    Public so tests can assert a memory is present *complete* rather than
    partially rendered (`test_composer_does_not_truncate_by_position`).
    """
    return config.LINE_TEMPLATE.format(content=_clean(item.content))


def render_block(items: Sequence[RankedCandidate]) -> str:
    """Render the full block, header included. Empty selection -> empty string.

    Returning `""` rather than a bare header for an empty selection is
    deliberate: a header promising "what you know about the user" followed by
    nothing invites the model to fill the gap, and costs tokens to do it.
    """
    if not items:
        return ""
    lines = [config.BLOCK_HEADER]
    lines.extend(render_line(item) for item in items)
    return config.LINE_SEPARATOR.join(lines)


# ---------------------------------------------------------------------------
# steps 8-10 — the drop loop
# ---------------------------------------------------------------------------

def _lowest_scored_index(items: Sequence[RankedCandidate]) -> int:
    """Index of the memory to drop: the LOWEST ranking score.

    Score, never position. The two coincide when the caller passed items in rank
    order — which is the normal case, and precisely why this is written to not
    depend on it.

    Ties break on `memory_id`, matching `rank()`'s `(-score, memory_id)` sort:
    among equal scores, the id that sorts *last* is the one ranking already
    considered least preferred, so it is the one to drop.

    An earlier version broke ties on list position ("drop the last of the tied
    items"), which is the same thing only when the caller already sorted. It
    was not: across the 720 permutations of six equal-scoring candidates, 672
    retained a different set — reversing the input kept the two highest ids
    instead of the two lowest. The score invariant held throughout, but the
    determinism this function's docstring promised did not. `compose()` ranks
    first and so never saw it; `compose_profile_block()` is exported and does.
    """
    lowest = min(item.score for item in items)
    tied = [(item.memory_id, index) for index, item in enumerate(items) if item.score == lowest]
    # max() over (memory_id, index): the highest memory_id among the tied. The
    # index rides along only to be returned, and to settle the impossible case
    # of two candidates sharing an id.
    return max(tied)[1]


def _fit(
    ranked: Sequence[RankedCandidate],
    budget: int,
) -> tuple[list[RankedCandidate], list[RankedCandidate], str, int]:
    """Drop lowest-scored memories until the rendered block fits `budget`.

    Returns `(selected, dropped, block, token_count)`.

    Terminates: every iteration removes exactly one element from a finite list,
    and the empty selection renders to `""`, which counts 0 tokens and satisfies
    any non-negative budget. Worst case — a budget of 0, or one memory larger
    than the whole budget — the loop empties the selection and returns an empty
    block (plan step 10).
    """
    selected = list(ranked)
    dropped: list[RankedCandidate] = []

    block = render_block(selected)
    tokens = count_tokens(block)

    while selected and tokens > budget:
        victim = selected.pop(_lowest_scored_index(selected))
        dropped.append(victim)
        logger.debug(
            "context composer: dropped %s (score %.6f) — block was %d tokens "
            "over a %d-token budget",
            victim.memory_id,
            victim.score,
            tokens,
            budget,
        )
        block = render_block(selected)
        tokens = count_tokens(block)

    return selected, dropped, block, tokens


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def compose_profile_block(
    ranked: Iterable[RankedCandidate],
    budget: int | None = None,
) -> ComposedContext:
    """Fit already-ranked memories into `budget` tokens (plan step 7).

    `budget` defaults to `CONTEXT_TOKEN_BUDGET` (512). A negative budget is
    treated as 0 rather than raising: a caller that computed a negative
    remaining budget has no room, and "no room" is a composable answer.
    """
    items = list(ranked or [])
    limit = config.token_budget() if budget is None else int(budget)
    limit = max(0, limit)

    # A memory whose content is blank renders as an empty bullet: it spends
    # budget and says nothing. Drop it before the budget loop ever sees it, so
    # it cannot displace a memory that does say something.
    items = [item for item in items if _clean(item.content)]

    if not items:
        # Empty candidate list -> empty block, no exception.
        return ComposedContext(block="", memory_ids=[], dropped_ids=[], token_count=0, budget=limit)

    selected, dropped, block, tokens = _fit(items, limit)

    if tokens > limit:  # pragma: no cover - `_fit` cannot return this
        raise AssertionError(
            f"composer produced a {tokens}-token block against a {limit}-token budget"
        )

    if dropped:
        logger.info(
            "context composer: kept %d/%d memories within %d tokens (used %d); dropped %s",
            len(selected),
            len(items),
            limit,
            tokens,
            ", ".join(f"{d.memory_id}@{d.score:.4f}" for d in dropped),
        )

    return ComposedContext(
        block=block,
        memory_ids=[item.memory_id for item in selected],
        dropped_ids=[item.memory_id for item in dropped],
        token_count=tokens,
        budget=limit,
        included=selected,
    )


def compose(
    candidates: Iterable[RankedCandidate | RetrievalCandidate],
    *,
    budget: int | None = None,
    top_k: int | None = None,
    now: datetime | None = None,
) -> ComposedContext:
    """Rank (if needed) and compose. The single entry point for M5 (step 12).

    Accepts either raw `RetrievalCandidate`s straight off `hybrid_search()` — in
    which case it ranks them first — or `RankedCandidate`s from a caller that
    already ranked. M5 has the former and should not have to know about the
    ranking node to get a prompt block; a caller that wants to inspect scores
    between the two stages can still call `rank()` itself and pass the result.

    Returns both the rendered block and the included memory ids.
    """
    items = list(candidates or [])
    if not items:
        return ComposedContext(
            block="",
            memory_ids=[],
            dropped_ids=[],
            token_count=0,
            budget=config.token_budget() if budget is None else max(0, int(budget)),
        )

    if all(isinstance(item, RankedCandidate) for item in items):
        ranked: list[RankedCandidate] = list(items)  # type: ignore[arg-type]
    elif any(isinstance(item, RankedCandidate) for item in items):
        raise TypeError(
            "compose() takes either all RankedCandidate or all RetrievalCandidate; "
            "a mixed list has no single meaning for the ranking step"
        )
    else:
        ranked = rank(items, top_k=top_k, now=now)  # type: ignore[arg-type]

    return compose_profile_block(ranked, budget)
