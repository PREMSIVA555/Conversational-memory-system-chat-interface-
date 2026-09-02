"""M8 unit tests — the eval fixture's embedding cache is shared across suites.

WHY THIS FILE EXISTS
--------------------
`seed()` used to prune the on-disk embedding cache against the corpus it was
seeding, so seeding v1 evicted every v2-only vector and vice versa. Measured: a
single v1 run took the cache from 63 entries to 44, dropping all 19 v2 rows, and
the next v2 run re-embedded them.

On a Voyage key metered at 3 requests per minute that is roughly a minute of
backoff per alternation between suites — and it is completely silent. Nothing
fails; the run just takes longer, which is the kind of cost nobody attributes to
a cache policy.

`all_corpus_texts()` was written specifically to prevent this and was never
called by anything. These tests pin both halves: the union helper is correct,
and `seed()` actually uses it.

Run:  pytest tests/unit/test_eval_fixture_cache.py -v
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.fixtures import seed_memories  # noqa: E402
from evals.fixtures.seed_memories import (  # noqa: E402
    ALL_CORPORA,
    MEMORIES,
    V2_MEMORIES,
    all_corpus_texts,
)


def test_all_corpus_texts_covers_every_generation():
    """The union must contain both corpora, or pruning still evicts one."""
    texts = set(all_corpus_texts())
    missing_v1 = {m.content for m in MEMORIES} - texts
    missing_v2 = {m.content for m in V2_MEMORIES} - texts
    assert not missing_v1, f"v1 rows absent from the union: {sorted(missing_v1)[:3]}"
    assert not missing_v2, f"v2 rows absent from the union: {sorted(missing_v2)[:3]}"


def test_all_corpus_texts_is_deduplicated():
    """v2 is a superset of v1, so a naive concatenation would double every row."""
    texts = all_corpus_texts()
    assert len(texts) == len(set(texts)), "all_corpus_texts returned duplicates"
    # v2 = v1 + 19, and v1 ⊂ v2, so the union is exactly v2's row count.
    assert len(texts) == len({m.content for m in V2_MEMORIES})


def test_every_registered_corpus_is_reachable_from_the_union():
    """A corpus added to ALL_CORPORA but not covered would be silently evicted."""
    for corpus in ALL_CORPORA:
        missing = {m.content for m in corpus} - set(all_corpus_texts())
        assert not missing, f"a registered corpus is not covered by the union: {missing}"


def test_seed_prunes_against_the_union_not_the_corpus_it_is_seeding():
    """`seed()` must call `prune_cache(all_corpus_texts())`, not `prune_cache(contents)`.

    Asserted against the source because the alternative — mocking a database
    session, the embedder and the cache to observe one argument — would test the
    mocks more than the policy. This project already uses a source-level check
    for the same reason (the tree-wide grep that forbids a provider-prefixed
    model literal outside `llm/config.py`).

    The failure this prevents is invisible at runtime: pruning against the
    seeded corpus is not an error, it just quietly re-embeds the other suite's
    rows on the next alternation.
    """
    # Strip comments and docstring prose before matching. The comment inside
    # seed() explains the old behaviour by naming it, and a naive substring
    # search reads that explanation as the code it warns about.
    source = "\n".join(
        line.split("#", 1)[0]
        for line in inspect.getsource(seed_memories.seed).splitlines()
    )
    assert "prune_cache(all_corpus_texts())" in source, (
        "seed() no longer prunes against the union of every corpus. Pruning "
        "against the corpus being seeded evicts the other suite's vectors and "
        "silently re-embeds them on the next run — a minute of backoff per "
        "alternation on a 3-request-per-minute key."
    )
    assert "prune_cache(contents)" not in source, (
        "seed() is pruning against the corpus being seeded again"
    )
