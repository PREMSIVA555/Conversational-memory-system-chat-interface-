"""M9 unit tests — the attribute-slot registry.

Pure functions, no database. What is being pinned here is a POLICY as much as
code: which slots supersede, which do not, and what happens to a slot name the
model invented.

Run:  pytest tests/unit/test_attributes.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capture.attributes import (  # noqa: E402
    KNOWN_ATTRIBUTES,
    MULTI_VALUED,
    SINGLE_VALUED,
    is_single_valued,
    normalise_attribute,
)


def test_the_two_sets_do_not_overlap():
    """A slot cannot be both single- and multi-valued.

    Overlap would make `is_single_valued` the tiebreaker by accident, and which
    set won would depend on nothing more than which membership test ran first.
    """
    assert SINGLE_VALUED & MULTI_VALUED == frozenset()
    assert KNOWN_ATTRIBUTES == SINGLE_VALUED | MULTI_VALUED


def test_the_slot_that_caused_the_bug_is_single_valued():
    """`default_programming_language` is the whole reason this module exists."""
    assert is_single_valued("default_programming_language")


@pytest.mark.parametrize(
    "attribute",
    ["food_preference", "dietary_restriction", "hobby", "allergy", "skill"],
)
def test_plural_slots_never_supersede(attribute):
    """The counter-example that rules out a similarity threshold.

    "The user likes dosa." and "The user likes idli." are near-identical in
    shape, vocabulary and embedding distance, and they are BOTH TRUE. A rule
    based on similarity destroys the dosa; a rule based on the attribute keeps
    it. If any of these ever moves into SINGLE_VALUED, real memories start
    disappearing.
    """
    assert attribute in KNOWN_ATTRIBUTES
    assert not is_single_valued(attribute)


def test_an_invented_slot_is_rejected_rather_than_stored():
    """The model may propose anything; only the registry is accepted.

    None means "ordinary fact, supersedes nothing", which is the safe failure
    direction. A missed supersession is a nuisance; a WRONGLY recognised one
    marks a true memory superseded and removes it from retrieval.
    """
    assert normalise_attribute("pet_species") is None
    assert normalise_attribute("favourite_colour") is None
    assert normalise_attribute("") is None
    assert normalise_attribute(None) is None
    assert normalise_attribute(42) is None
    assert normalise_attribute(["default_programming_language"]) is None


@pytest.mark.parametrize(
    "raw",
    [
        "default_programming_language",
        "  default_programming_language  ",
        "Default_Programming_Language",
        "default programming language",
        "default-programming-language",
    ],
)
def test_slot_names_are_normalised_before_matching(raw):
    """Case, padding, spaces and hyphens are the model's, not the registry's.

    Rejecting `Default Programming Language` would silently cost a supersession
    for a purely cosmetic reason, which is the kind of near-miss that is very
    hard to notice from the outside.
    """
    assert normalise_attribute(raw) == "default_programming_language"


def test_normalisation_cannot_invent_membership():
    """Normalising must not turn an unknown slot into a known one."""
    assert normalise_attribute("default programming languages") is None  # plural
    assert normalise_attribute("programming_language") is None  # not the DEFAULT one


def test_known_and_default_language_slots_are_distinct():
    """`programming_language_known` is plural; the DEFAULT one is not.

    Knowing five languages is ordinary; wanting code in five at once is the bug
    this milestone fixes. The two slots are deliberately separate.
    """
    assert is_single_valued("default_programming_language")
    assert not is_single_valued("programming_language_known")


def test_is_single_valued_is_total():
    """It is called on whatever the extractor produced, so it must not raise."""
    for value in (None, "", "unknown_slot", 0, [], {}, object()):
        assert is_single_valued(value) is False
