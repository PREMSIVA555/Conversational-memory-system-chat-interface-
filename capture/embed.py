"""Node 4 -- batch-embed the surviving candidates (plan step 5).

The whole point of this node is the word *batch*. N candidates produce exactly
**one** `llm.config.embed()` call, never N -- that is a provider round-trip per
fact saved on every single turn, and `test_embed_node_batches_candidates` is
the regression guard.

`llm_config.embed` is reached through the module (`llm_config.embed(...)`)
rather than imported by name, so a monkeypatch on `llm.config.embed` is
actually observed here. A `from llm.config import embed` would bind the
original function at import time and quietly defeat that test.
"""

from __future__ import annotations

import asyncio
from typing import Any

from llm import config as llm_config

from capture.metrics import log_warning, node_span
from graphs.capture_state import Candidate, CaptureState

#: Backoff schedule for a rate-limited or transient embedding call, in seconds.
#:
#: Embedding providers rate-limit per minute, and this project's configured
#: provider throttles hard on the free tier (3 requests/minute without a
#: payment method on file). LiteLLM's own `num_retries` fires again within
#: milliseconds, which cannot clear a per-minute window -- so a burst of
#: captures loses its embeddings and silently writes nothing.
#:
#: These delays straddle a minute boundary on purpose. Capture is asynchronous
#: and off the request path, so waiting is free here in a way it never would be
#: on a user's reply: the correct trade for a background job is to be slow and
#: complete rather than fast and lossy.
#: The schedule spans several minute-windows because the throttle is per
#: minute and may be shared: other processes on the same API key (a parallel
#: test run, a second worker) consume the same quota, so one clear window is
#: not guaranteed to be *this* caller's.
EMBED_RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0, 30.0, 45.0, 60.0, 60.0, 60.0)

#: Substrings marking a failure that is worth waiting out rather than dropping.
_RETRYABLE_MARKERS = (
    "rate limit",
    "ratelimit",
    "rate_limit",
    "429",
    " rpm",
    " tpm",
    "too many requests",
    "timeout",
    "timed out",
    "connection",
    "temporarily unavailable",
    "service unavailable",
    "502",
    "503",
    "529",
)


class EmbeddingUnavailable(RuntimeError):
    """The embedding provider could not be reached, or refused, for this batch.

    Raised rather than returning `[]` so a quota exhaustion is distinguishable
    from "there was nothing to embed". Both used to produce an empty candidate
    list, which short-circuited the graph to END and surfaced downstream as the
    confusing "no memory row appeared" -- a symptom several steps removed from
    the cause. The worker now records the job as `failed` with this message
    attached, and `memsys_capture_jobs_total{status="failed"}` moves.
    """


def is_retryable(exc: BaseException) -> bool:
    """True for throttling and transient transport failures.

    Matches on the message rather than the exception class because LiteLLM
    collapses every provider's error taxonomy into a small set of wrapper types
    and puts the distinguishing detail in the string.
    """
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _RETRYABLE_MARKERS)


async def embed_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Attach an embedding vector to each candidate. Exactly one provider call.

    "One call" is per *attempt*: the batch is retried whole on a throttling
    error, so the N-candidates-to-one-call property that
    `test_embed_node_batches_candidates` asserts always holds.

    A candidate whose vector comes back the wrong width, or missing, is dropped
    rather than inserted with a null embedding: an unembedded row is invisible
    to dedup and to M3 retrieval, so it would be a silent write-only memory.
    """
    if not candidates:
        return []

    texts = [c.text for c in candidates]

    vectors: list[list[float]] | None = None
    for attempt in range(len(EMBED_RETRY_DELAYS) + 1):
        try:
            vectors = await llm_config.embed(texts)  # <- the single batched call
            break
        except Exception as exc:
            retryable = is_retryable(exc)
            if attempt >= len(EMBED_RETRY_DELAYS) or not retryable:
                log_warning(
                    "capture.embed.provider_error",
                    attempt=attempt,
                    retryable=retryable,
                    exhausted=retryable,
                    error=f"{type(exc).__name__}: {exc}",
                )
                reason = (
                    f"embedding retries exhausted after {attempt + 1} attempts"
                    if retryable
                    else "embedding provider rejected the batch"
                )
                raise EmbeddingUnavailable(
                    f"{reason}: {type(exc).__name__}: {exc}"
                ) from exc
            delay = EMBED_RETRY_DELAYS[attempt]
            log_warning(
                "capture.embed.throttled",
                attempt=attempt,
                retry_in_seconds=delay,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
            await asyncio.sleep(delay)

    if vectors is None:  # pragma: no cover - loop always breaks or returns
        return []

    expected_dim = llm_config.resolve_embedding_dim()
    out: list[Candidate] = []
    for index, candidate in enumerate(candidates):
        vector = vectors[index] if index < len(vectors) else None
        if not vector:
            log_warning("capture.embed.missing_vector", text=candidate.text[:120])
            continue
        if len(vector) != expected_dim:
            log_warning(
                "capture.embed.dimension_mismatch",
                expected=expected_dim,
                actual=len(vector),
                text=candidate.text[:120],
            )
            continue
        out.append(candidate.with_(embedding=[float(x) for x in vector]))
    return out


async def embed_node(state: CaptureState) -> dict[str, Any]:
    """LangGraph node: state['scored'] -> state['embedded']."""
    subject_id = state.get("subject_id", "")
    candidates = state.get("scored") or []

    with node_span("embed", subject_id, n_in=len(candidates)) as span:
        embedded = await embed_candidates(candidates)
        span["out"] = len(embedded)
        span["provider_calls"] = 1 if candidates else 0

    return {"embedded": embedded}
