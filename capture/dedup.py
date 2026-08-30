"""Node 5 -- mark each candidate `new` or `duplicate_of=<id>` (plan step 6).

For every candidate this runs a pgvector cosine query against the existing
non-deleted `memories` rows for that **subject**, and compares the best match
against `CAPTURE_DEDUP_COSINE_THRESHOLD`.

SCOPE ------------------------------------------------------------------------
The similarity query filters on `subject_id` AND `deleted_at IS NULL` (see
`store/memories.py:find_similar`), and it runs inside an RLS session opened for
this subject. Subject A's memories are therefore invisible to subject B's dedup
pass at two independent layers -- `test_dedup_scoped_to_subject_id` is the guard.

ADVISORY, NOT AUTHORITATIVE ---------------------------------------------------
This node reads outside any lock, so its verdict can be stale by the time the
write happens. It is kept because it is what makes dedup *observable* (the
verdict and the similarity land in state and in the metrics), but the binding
decision is re-taken under an advisory lock in
`store/memories.py:persist_candidates`. Anything else would be a read-then-write
race, and `test_concurrent_identical_turns_do_not_double_write` would catch it.
"""

from __future__ import annotations

from typing import Any

from store.db import session
from store.memories import find_similar

from capture import config as capture_config
from capture.metrics import DEDUP_OUTCOMES, node_span
from graphs.capture_state import Candidate, CaptureState


def classify(similarity: float | None, threshold: float) -> str:
    """`duplicate` at or above the threshold, else `new`."""
    if similarity is None:
        return "new"
    return "duplicate" if similarity >= threshold else "new"


async def dedup_candidates(
    subject_id: str,
    actor_id: str,
    candidates: list[Candidate],
    *,
    threshold: float | None = None,
) -> list[Candidate]:
    """Annotate each candidate with `dedup_status`, `duplicate_of`, `similarity`."""
    if not candidates:
        return []

    limit = threshold if threshold is not None else capture_config.dedup_cosine_threshold()
    out: list[Candidate] = []

    async with session(subject_id, actor_id) as conn:
        for candidate in candidates:
            if not candidate.embedding:
                out.append(candidate.with_(dedup_status="new", duplicate_of=None, similarity=None))
                continue

            rows = await find_similar(conn, subject_id, candidate.embedding, limit=1)
            best = rows[0] if rows else None
            similarity = float(best["similarity"]) if best and best["similarity"] is not None else None
            status = classify(similarity, limit)
            out.append(
                candidate.with_(
                    dedup_status=status,
                    duplicate_of=str(best["id"]) if status == "duplicate" and best else None,
                    similarity=similarity,
                )
            )

    return out


async def dedup_node(state: CaptureState) -> dict[str, Any]:
    """LangGraph node: annotates state['embedded'] in place (as a new list)."""
    subject_id = state.get("subject_id", "")
    actor_id = state.get("actor_id", subject_id)
    candidates = state.get("embedded") or []

    with node_span("dedup", subject_id, n_in=len(candidates)) as span:
        annotated = await dedup_candidates(subject_id, actor_id, candidates)
        for candidate in annotated:
            DEDUP_OUTCOMES.labels(outcome=candidate.dedup_status or "new").inc()
        span["out"] = len(annotated)
        span["threshold"] = capture_config.dedup_cosine_threshold()
        span["verdicts"] = [
            {"text": c.text, "status": c.dedup_status, "similarity": c.similarity}
            for c in annotated
        ]

    return {"embedded": annotated}
