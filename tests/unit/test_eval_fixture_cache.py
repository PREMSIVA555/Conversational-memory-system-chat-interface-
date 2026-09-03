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

from evals import run_eval  # noqa: E402
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


# ---------------------------------------------------------------------------
# the QUERY cache — the same defect, found later
# ---------------------------------------------------------------------------
#
# The corpus cache was fixed first and these tests only covered that one. An
# independent verifier then measured the query cache doing exactly the same
# thing: a v1 run took `query_embedding_cache.json` from 19 entries to 9, and
# the next v2 run printed "10 newly embedded in one batched request" — spending
# a live request on a rate-limited provider to recover vectors it had just
# discarded.
#
# The lesson worth keeping: fixing one caller of a bad pattern is not fixing the
# pattern. `prune_cache(contents)` and `prune_persistent_cache(suite_queries)`
# are the same mistake in two files, and only one of them had a test.


def test_all_suite_queries_covers_every_registered_suite():
    """The union must contain every suite's queries, or pruning evicts one."""
    union = set(run_eval.all_suite_queries())
    for name in run_eval.SUITES:
        path = run_eval.resolve_suite(name)
        missing = {r["query"] for r in run_eval.load_suite(path)} - union
        assert not missing, f"{name} queries absent from the union: {sorted(missing)[:3]}"


def test_all_suite_queries_is_deduplicated():
    """v2 carries all nine v1 queries verbatim, so a concatenation would double them."""
    queries = run_eval.all_suite_queries()
    assert len(queries) == len(set(queries)), "all_suite_queries returned duplicates"


def test_run_eval_prunes_the_query_cache_against_every_suite():
    """`run()` must prune with `all_suite_queries()`, not this run's queries.

    Source-level for the same reason as the corpus check above, and with the
    same comment-stripping so the explanatory comment naming the old call is not
    mistaken for the call itself.
    """
    source = chr(10).join(
        line.split("#", 1)[0] for line in inspect.getsource(run_eval.run).splitlines()
    )
    assert "prune_persistent_cache(all_suite_queries())" in source, (
        "run() no longer prunes the query cache against the union of all suites; "
        "pruning against one suite's queries evicts the other's vectors and "
        "silently re-embeds them on the next alternation"
    )
    assert "prune_persistent_cache(suite_queries)" not in source, (
        "run() is pruning the query cache against only this run's queries again"
    )


def test_neither_cache_prunes_against_a_single_corpus_or_suite():
    """The pattern, not the instance.

    Two callers had the identical defect and only one was covered by a test, so
    the second survived the first fix. This asserts the shape in both places at
    once, so a third caller written the same way has somewhere obvious to fail.
    """
    for func in (seed_memories.seed, run_eval.run):
        source = chr(10).join(
            line.split("#", 1)[0] for line in inspect.getsource(func).splitlines()
        )
        for bad in ("prune_cache(contents)", "prune_persistent_cache(suite_queries)"):
            assert bad not in source, (
                f"{func.__qualname__} prunes a shared cache against one "
                f"generation's texts via `{bad}` — the other generation's "
                f"vectors are evicted and silently re-embedded"
            )
