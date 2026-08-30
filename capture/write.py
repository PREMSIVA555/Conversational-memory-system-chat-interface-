"""Node 6 -- the terminal node: insert or reinforce (plan step 9).

Dispatch itself lives in `store/memories.py:persist_candidates`, because the
insert-or-reinforce choice has to be made inside the same transaction and the
same advisory lock as the similarity read. This node's job is to call that,
record the outcome in state, and emit metrics.
"""

from __future__ import annotations

from typing import Any

from store.memories import persist_candidates

from capture.metrics import WRITES, log_warning, node_span
from graphs.capture_state import CaptureState


async def write_node(state: CaptureState) -> dict[str, Any]:
    """LangGraph node: state['embedded'] -> state['write_results']."""
    subject_id = state.get("subject_id", "")
    actor_id = state.get("actor_id", subject_id)
    candidates = state.get("embedded") or []

    with node_span("write", subject_id, n_in=len(candidates)) as span:
        results = await persist_candidates(subject_id, actor_id, candidates)

        for result in results:
            WRITES.labels(action=result["action"]).inc()
            # The dedup node reads outside the lock; the write re-reads inside
            # it. A disagreement is expected under concurrency and is exactly
            # the case the lock exists to handle -- log it so it is visible
            # rather than silent.
            from_node = result.get("dedup_status_from_node")
            at_write = result.get("dedup_status_at_write")
            if from_node is not None and from_node != at_write:
                log_warning(
                    "capture.write.dedup_verdict_changed_under_lock",
                    subject_id=subject_id,
                    from_node=from_node,
                    at_write=at_write,
                    memory_id=result.get("memory_id"),
                )

        span["out"] = len(results)
        span["inserted"] = sum(1 for r in results if r["action"] == "insert")
        span["reinforced"] = sum(1 for r in results if r["action"] == "reinforce")
        span["memory_ids"] = [r["memory_id"] for r in results]

    return {"write_results": results}
