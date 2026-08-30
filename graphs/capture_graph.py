"""The capture graph (plan step 10): extract -> pii -> evaluate -> embed -> dedup -> write.

The order is fixed and load-bearing, not stylistic:

* `pii` sits immediately after `extract` and before every node that can touch
  the database, so unredacted text has no path to the `content` column.
* `evaluate` precedes `embed` so the confidence floor drops candidates *before*
  they cost a provider round-trip.
* `dedup` precedes `write` so the write node knows what it is dispatching.

SHORT-CIRCUIT ----------------------------------------------------------------
Every hop is a conditional edge, not a plain one. After each node the gate reads
that node's own output key and routes to `END` the moment it is empty. Most
turns contain nothing memorable, so the common path is `extract -> END` with one
LLM call and zero database work -- an unconditional chain would pay for an
embedding call and two queries on every "thanks".

`END` is reachable from all six nodes; `write` also terminates unconditionally.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from langgraph.graph import END, StateGraph

from capture import config as capture_config
from capture.dedup import dedup_node
from capture.embed import embed_node
from capture.evaluate import evaluate_node
from capture.extract import extract_node
from capture.metrics import log_event
from capture.pii import pii_node
from capture.write import write_node
from graphs.capture_state import CaptureState, Turn, new_state

#: The canonical pipeline order. `test_capture_graph_node_order` asserts the
#: compiled graph's edges match this exactly.
NODE_ORDER: tuple[str, ...] = ("extract", "pii", "evaluate", "embed", "dedup", "write")

NODE_FUNCS: dict[str, Callable] = {
    "extract": extract_node,
    "pii": pii_node,
    "evaluate": evaluate_node,
    "embed": embed_node,
    "dedup": dedup_node,
    "write": write_node,
}

#: The state key each node populates -- also the key its short-circuit gate reads.
OUTPUT_KEY: dict[str, str] = {
    "extract": "candidates",
    "pii": "redacted",
    "evaluate": "scored",
    "embed": "embedded",
    "dedup": "embedded",
}

CONTINUE = "continue"
STOP = "end"


def _gate(state_key: str) -> Callable[[CaptureState], str]:
    """Route onward while `state_key` holds candidates, else straight to END."""

    def decide(state: CaptureState) -> str:
        return CONTINUE if state.get(state_key) else STOP

    decide.__name__ = f"gate_on_{state_key}"
    return decide


def build_capture_graph() -> StateGraph:
    """Assemble (but do not compile) the capture StateGraph."""
    builder = StateGraph(CaptureState)

    for name in NODE_ORDER:
        builder.add_node(name, NODE_FUNCS[name])

    builder.set_entry_point(NODE_ORDER[0])

    for source, target in zip(NODE_ORDER, NODE_ORDER[1:]):
        builder.add_conditional_edges(
            source,
            _gate(OUTPUT_KEY[source]),
            {CONTINUE: target, STOP: END},
        )

    builder.add_edge(NODE_ORDER[-1], END)
    return builder


_compiled: Any = None


def get_capture_graph() -> Any:
    """Compile once and reuse. Compilation is pure structure -- no I/O, no state."""
    global _compiled
    if _compiled is None:
        _compiled = build_capture_graph().compile()
    return _compiled


async def run_capture(
    subject_id: str,
    actor_id: str,
    turn: Turn,
    *,
    timeout: float | None = None,
) -> CaptureState:
    """Run one turn through the graph and return the final state.

    Raises `asyncio.TimeoutError` if the run exceeds `CAPTURE_TIMEOUT_SECONDS`.
    The caller (`capture/worker.py`) owns that failure; nothing on the request
    path ever awaits this.
    """
    budget = timeout if timeout is not None else capture_config.capture_timeout_seconds()
    graph = get_capture_graph()
    initial = new_state(subject_id, actor_id, turn)

    final: CaptureState = await asyncio.wait_for(graph.ainvoke(initial), timeout=budget)

    log_event(
        "capture.run.complete",
        subject_id=subject_id,
        candidates=len(final.get("candidates") or []),
        scored=len(final.get("scored") or []),
        embedded=len(final.get("embedded") or []),
        writes=len(final.get("write_results") or []),
        actions=[r.get("action") for r in (final.get("write_results") or [])],
    )
    return final
