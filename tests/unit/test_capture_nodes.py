"""M2 unit tests for individual capture nodes and the compiled graph's shape.

These exercise the node seams directly rather than through the database. The
capture nodes are deliberately thin wrappers around module-level helpers
(`extract_candidates`, `score_candidates`, `persist_candidates`), and those
helpers are looked up as module globals at call time -- so monkeypatching one
reaches the *compiled* graph too, without rebuilding it.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from capture import config as capture_config
from capture import embed as embed_module
from capture import evaluate as evaluate_module
from capture import extract as extract_module
from capture import write as write_module
from graphs.capture_graph import NODE_ORDER, get_capture_graph
from graphs.capture_state import Candidate, new_state
from llm import config as llm_config

#: Liveness backstop. These are fast (only the extract test touches a provider),
#: so anything near this bound is a hang, not slowness.
pytestmark = pytest.mark.timeout(300)


# ---------------------------------------------------------------------------
# test_extract_returns_empty_for_nonmemorable_turn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message", ["hi", "thanks"])
async def test_extract_returns_empty_for_nonmemorable_turn(message, monkeypatch, caplog):
    """A greeting yields zero candidates, and the graph reaches END without writing.

    Three halves, all real:

    1. The live extractor is called on the turn. Nothing in `extract.py`
       stoplists greetings -- deciding "not memorable" is the model's job -- so
       this genuinely exercises the prompt.
    2. **The empty result is proved non-vacuous.** `extract_candidates()`
       deliberately swallows provider errors and returns `[]`, so a throttled or
       unreachable provider would make `assert candidates == []` pass while the
       extraction path was completely broken -- and this project runs against a
       rate-limited provider, so that is a live risk rather than a theoretical
       one. Two independent guards distinguish "the model ran and correctly
       found nothing" from "the model never ran": the provider must actually
       have been called and returned non-empty text, and no
       `capture.extract.provider_error` may have been logged.
    3. The compiled graph is run on the same turn. `write_results` must be
       empty, and `persist_candidates` must never be reached at all. A sentinel
       replaces it: if the short-circuit edge were missing, the sentinel would
       fire and fail the test rather than silently writing nothing.
    """
    turn = {"user": message, "assistant": "Hello! How can I help?"}

    provider_responses: list[str] = []
    real_complete = llm_config.complete

    async def spy_complete(*args, **kwargs):
        response = await real_complete(*args, **kwargs)
        provider_responses.append(response)
        return response

    monkeypatch.setattr(llm_config, "complete", spy_complete)

    with caplog.at_level(logging.WARNING, logger="memsys.capture"):
        candidates = await extract_module.extract_candidates(turn)

    assert candidates == [], f"expected no memorable facts in {message!r}, got {candidates}"

    # -- the empty list came from a model that actually answered --------------
    assert len(provider_responses) == 1, (
        f"extractor made {len(provider_responses)} provider calls, expected exactly 1 -- "
        "an empty candidate list is only meaningful if the model was really asked"
    )
    assert provider_responses[0].strip(), (
        "the provider returned empty content, so the empty candidate list proves "
        "nothing about the model's judgement"
    )

    errors = [r.getMessage() for r in caplog.records if "capture.extract.provider_error" in r.getMessage()]
    assert not errors, f"extraction failed rather than finding nothing memorable: {errors}"

    reached_write = False

    async def sentinel(*args, **kwargs):
        nonlocal reached_write
        reached_write = True
        return []

    monkeypatch.setattr(write_module, "persist_candidates", sentinel)

    final = await get_capture_graph().ainvoke(new_state("s", "s", turn))

    assert final["candidates"] == []
    assert final["write_results"] == []
    assert not reached_write, "graph must short-circuit to END, not reach the write node"


async def test_extract_provider_failure_is_logged_not_silent(monkeypatch, caplog):
    """A dead provider still returns [], but says so -- proving the guard above works.

    This is the control for `test_extract_returns_empty_for_nonmemorable_turn`:
    it forces the exact failure mode that would otherwise produce a vacuous
    pass, and asserts the `capture.extract.provider_error` signal that test
    relies on actually fires. Without this, the guard could itself be dead code.
    """

    async def unavailable(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(llm_config, "complete", unavailable)

    with caplog.at_level(logging.WARNING, logger="memsys.capture"):
        candidates = await extract_module.extract_candidates(
            {"user": "I'm allergic to peanuts.", "assistant": "Noted."}
        )

    assert candidates == [], "a provider failure must not fabricate candidates"
    assert any(
        "capture.extract.provider_error" in record.getMessage() for record in caplog.records
    ), "a provider failure must be logged, not swallowed silently"


# ---------------------------------------------------------------------------
# test_low_confidence_candidate_is_dropped
# ---------------------------------------------------------------------------


async def test_low_confidence_candidate_is_dropped(monkeypatch):
    """A candidate scored below the floor never reaches the write node.

    The scorer is stubbed so the confidence is exact and the assertion is about
    the *floor logic*, not about the model's opinion. Everything after the stub
    -- the filter, the short-circuit edge, the write node -- is the real code.
    """
    floor = capture_config.confidence_floor()
    low = round(floor - 0.2, 3)
    assert low >= 0.0

    async def stub_scores(candidates):
        return [c.with_(importance=0.9, confidence=low) for c in candidates]

    async def stub_extract(turn, **kwargs):
        return [Candidate(text="The user might possibly enjoy sailing.", source="user_statement")]

    reached_write = False

    async def sentinel(*args, **kwargs):
        nonlocal reached_write
        reached_write = True
        return []

    monkeypatch.setattr(evaluate_module, "score_candidates", stub_scores)
    monkeypatch.setattr(extract_module, "extract_candidates", stub_extract)
    monkeypatch.setattr(write_module, "persist_candidates", sentinel)

    final = await get_capture_graph().ainvoke(
        new_state("s", "s", {"user": "I might like sailing.", "assistant": "Noted."})
    )

    assert len(final["candidates"]) == 1, "the candidate must exist before evaluate"
    assert len(final["redacted"]) == 1, "and must survive the PII node"
    assert final["scored"] == [], f"confidence {low} < floor {floor} must be dropped"
    assert final["write_results"] == []
    assert not reached_write, "a sub-floor candidate must never reach the write node"

    dropped = final["dropped"]
    assert len(dropped) == 1
    assert dropped[0]["reason"] == "below_confidence_floor"
    assert dropped[0]["confidence"] == low


async def test_confidence_floor_boundary_is_inclusive():
    """A candidate scoring exactly the floor is kept -- `>=`, not `>`."""
    at_floor = Candidate(text="edge", confidence=0.5)
    below = Candidate(text="under", confidence=0.4999)

    kept, dropped = evaluate_module.apply_confidence_floor([at_floor, below], floor=0.5)

    assert [c.text for c in kept] == ["edge"]
    assert [d["text"] for d in dropped] == ["under"]


# ---------------------------------------------------------------------------
# test_embed_node_batches_candidates
# ---------------------------------------------------------------------------


async def test_embed_node_batches_candidates(monkeypatch):
    """N candidates cost exactly ONE embed() call, not N."""
    dim = llm_config.resolve_embedding_dim()
    calls: list[list[str]] = []

    async def counting_embed(texts, **kwargs):
        batch = list(texts)
        calls.append(batch)
        return [[float(index + 1)] * dim for index, _ in enumerate(batch)]

    monkeypatch.setattr(llm_config, "embed", counting_embed)

    candidates = [
        Candidate(text=f"The user fact number {n}.", confidence=0.9, importance=0.6)
        for n in range(5)
    ]

    result = await embed_module.embed_node(
        {"subject_id": "s", "actor_id": "s", "scored": candidates}
    )

    assert len(calls) == 1, f"expected 1 batched embed() call for 5 candidates, got {len(calls)}"
    assert calls[0] == [c.text for c in candidates], "all 5 texts must go in that one call"

    embedded = result["embedded"]
    assert len(embedded) == 5
    assert all(c.embedding is not None and len(c.embedding) == dim for c in embedded)
    # Order preserved: candidate i got vector i.
    assert [c.embedding[0] for c in embedded] == [1.0, 2.0, 3.0, 4.0, 5.0]


async def test_embed_node_makes_no_call_for_empty_input(monkeypatch):
    """Zero candidates cost zero provider calls."""
    calls = 0

    async def counting_embed(texts, **kwargs):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(llm_config, "embed", counting_embed)
    result = await embed_module.embed_node({"subject_id": "s", "actor_id": "s", "scored": []})

    assert calls == 0
    assert result["embedded"] == []


async def test_exhausted_embed_retries_raise_rather_than_returning_empty(monkeypatch):
    """A quota failure is distinguishable from "there was nothing to embed".

    Both used to produce `[]`, which short-circuited the graph to END and
    surfaced downstream as "no memory row appeared" -- a symptom several steps
    from the cause. An unembeddable batch now raises `EmbeddingUnavailable`, so
    the worker records the job as failed with the provider's message attached.
    """
    monkeypatch.setattr(embed_module, "EMBED_RETRY_DELAYS", (0.0, 0.0))
    attempts = 0

    async def throttled(texts, **kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("rate limit exceeded: 3 RPM")

    monkeypatch.setattr(llm_config, "embed", throttled)

    with pytest.raises(embed_module.EmbeddingUnavailable, match="retries exhausted"):
        await embed_module.embed_candidates([Candidate(text="The user sails.")])

    assert attempts == 3, f"expected 1 initial attempt + 2 retries, got {attempts}"


async def test_non_retryable_embed_error_raises_immediately(monkeypatch):
    """A non-retryable rejection fails fast instead of burning the backoff schedule."""
    attempts = 0

    async def rejected(texts, **kwargs):
        nonlocal attempts
        attempts += 1
        raise ValueError("invalid input encoding")

    monkeypatch.setattr(llm_config, "embed", rejected)

    with pytest.raises(embed_module.EmbeddingUnavailable, match="rejected the batch"):
        await embed_module.embed_candidates([Candidate(text="The user sails.")])

    assert attempts == 1, "a non-retryable error must not be retried"


async def test_embed_node_drops_wrong_width_vectors(monkeypatch):
    """A vector of the wrong dimension is dropped, never written with a bad embedding."""

    async def bad_embed(texts, **kwargs):
        return [[0.1] * 7 for _ in texts]

    monkeypatch.setattr(llm_config, "embed", bad_embed)
    result = await embed_module.embed_node(
        {"subject_id": "s", "actor_id": "s", "scored": [Candidate(text="x")]}
    )
    assert result["embedded"] == []


# ---------------------------------------------------------------------------
# test_capture_graph_node_order
# ---------------------------------------------------------------------------


def test_capture_graph_node_order():
    """The compiled graph's edges are exactly extract -> pii -> evaluate -> embed -> dedup -> write.

    Inspects the *compiled* graph rather than the `NODE_ORDER` constant, so the
    assertion would still fail if the constant and the wiring drifted apart.
    """
    expected = ("extract", "pii", "evaluate", "embed", "dedup", "write")
    assert NODE_ORDER == expected

    drawn = get_capture_graph().get_graph()

    nodes = [n for n in drawn.nodes if not n.startswith("__")]
    assert nodes == list(expected), f"graph nodes are {nodes}"

    edges = {(edge.source, edge.target) for edge in drawn.edges}

    # entry point
    assert ("__start__", "extract") in edges

    # the pipeline runs in exactly this order, one hop at a time
    for source, target in zip(expected, expected[1:]):
        assert (source, target) in edges, f"missing edge {source} -> {target}"

    # ...and nowhere else: no node may skip ahead to a later stage
    for i, source in enumerate(expected):
        for j, target in enumerate(expected):
            if j > i + 1:
                assert (source, target) not in edges, f"illegal skip edge {source} -> {target}"
            if j < i:
                assert (source, target) not in edges, f"illegal back edge {source} -> {target}"

    # every stage can short-circuit; write terminates
    for name in expected[:-1]:
        assert (name, "__end__") in edges, f"{name} has no short-circuit edge to END"
    assert ("write", "__end__") in edges


def test_capture_graph_compiles_once():
    """`get_capture_graph()` caches -- nodes are not rebound per invocation."""
    assert get_capture_graph() is get_capture_graph()
