"""The attribute slots a memory can fill, and which of them hold one value.

WHY A CLOSED LIST AND NOT A SIMILARITY THRESHOLD
------------------------------------------------
The obvious way to decide "does this new fact replace that old one?" is to look
at how similar they are. It does not work, and the counter-example is short:

    "The user prefers Java."          vs  "The user prefers c++."      REPLACES
    "The user likes dosa."            vs  "The user likes idli."       DOES NOT

Both pairs are near-identical in shape, vocabulary and embedding distance. What
separates them is not the sentences but the ATTRIBUTE underneath: a person has
one default programming language and many foods they enjoy. Similarity cannot
see that, so a threshold-based rule silently destroys real facts — you would
lose the dosa.

So supersession is driven by an explicit, closed registry of slots. The
extractor proposes one; anything not on this list is discarded and the memory is
stored with `attribute = NULL`, which means "ordinary fact, supersedes nothing".
That default is the safe direction: an unrecognised slot costs a missed
supersession, while a wrongly-recognised one costs a lost memory.

ADDING A SLOT
-------------
Two questions, in order:

  1. Does a person have exactly ONE of these at a time? If the answer is "well,
     usually" then it is NOT single-valued. `SINGLE_VALUED` is for slots where a
     second live value is a contradiction, not a nuance.
  2. Will the extractor reliably recognise it? A slot the model never emits is
     dead weight; a slot it emits for the wrong facts is a memory-shredder.

When in doubt, add it to `MULTI_VALUED` (documented, no supersession) rather
than to `SINGLE_VALUED`. Being wrong in that direction is recoverable.
"""

from __future__ import annotations

__all__ = [
    "SINGLE_VALUED",
    "MULTI_VALUED",
    "KNOWN_ATTRIBUTES",
    "normalise_attribute",
    "is_single_valued",
]


#: Slots where a new value CONTRADICTS the old one, so the old is superseded.
#:
#: Every entry here is a claim that a person has exactly one at a time. Read
#: each as a sentence — "a person has one default programming language" — and if
#: that sentence needs a qualifier, the slot belongs in MULTI_VALUED instead.
SINGLE_VALUED: frozenset[str] = frozenset(
    {
        # The slot that produced the bug this module exists for.
        "default_programming_language",
        # Communication preferences: a person has one preferred style at a time.
        "preferred_response_language",
        "preferred_response_format",
        "preferred_code_comment_style",
        # Situation. All of these change, and when they do the old value is
        # wrong rather than merely older.
        "employer",
        "job_title",
        "city_of_residence",
        "timezone",
        "preferred_name",
    }
)

#: Slots that are genuinely plural, listed so the extractor has somewhere honest
#: to put them and so a future reader can see they were CONSIDERED and rejected
#: for supersession rather than forgotten.
#:
#: A memory carrying one of these is stored with its attribute intact — useful
#: for grouping and for the panel — but it supersedes nothing.
MULTI_VALUED: frozenset[str] = frozenset(
    {
        "food_preference",       # you can like both dosa and idli
        "dietary_restriction",   # you can be lactose intolerant AND coeliac
        "hobby",
        "programming_language_known",  # distinct from the DEFAULT one
        "allergy",
        "skill",
    }
)

KNOWN_ATTRIBUTES: frozenset[str] = SINGLE_VALUED | MULTI_VALUED


def normalise_attribute(raw: object) -> str | None:
    """Map whatever the model proposed onto the registry, or None.

    Returns None for anything unrecognised, which stores the memory as an
    ordinary fact that supersedes nothing. That is the deliberate failure
    direction: a slot we fail to recognise costs one missed supersession, while
    a slot we wrongly recognise marks a true memory superseded and takes it out
    of retrieval. The first is a nuisance; the second loses information.
    """
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().lower().replace(" ", "_").replace("-", "_")
    return cleaned if cleaned in KNOWN_ATTRIBUTES else None


def is_single_valued(attribute: object) -> bool:
    """True when filling this slot should supersede whatever filled it before."""
    return isinstance(attribute, str) and attribute in SINGLE_VALUED
