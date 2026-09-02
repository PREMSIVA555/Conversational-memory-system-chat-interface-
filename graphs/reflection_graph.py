"""The reflection graph (M8 step 8): select -> summarize -> pii -> embed -> write.

REUSING M2'S NODES, NOT COPYING THEM
-----------------------------------
`pii` and `embed` below are **`capture.pii.pii_node` and
`capture.embed.embed_node` themselves**, not reimplementations. A reflection
summary is a sentence about the user that is about to be written to the
`content` column and to the `embedding` column, which is precisely what those
two nodes exist to guard, and a second scrubbing path would be a second place
for the redaction rules to drift.

The only work done here is an adapter, because those nodes read fixed
`CaptureState` keys — `pii_node` reads `candidates` and writes `redacted`,
`embed_node` reads `scored` and writes `embedded`. The adapters build a
one-element `CaptureState` around the summary sentence and unpack the result.
The alternative (renaming the reflection state's keys to match) would have made
this file read as if it were a capture run, which it is not.

The consequence is the property `test_reflection_summary_is_pii_filtered_and_
embedded` asserts: if the summarizer echoes an SSN out of a source memory, the
stored `content` contains `<US_SSN>` and not the digits — because the same
Presidio engine that guards capture guards this.

TRANSACTIONS: TWO, NOT ONE
--------------------------
`select` runs in its own short transaction, then `summarize` makes a completion
call that takes seconds, then `write` opens a fresh transaction. Spanning one
transaction across the provider call would hold `idle in transaction` for the
whole round-trip — on a bad day, for a whole rate-limit backoff — pinning a
connection and blocking vacuum on `memories` for no benefit.

The race that split introduces (another run consolidating the same sources in
between) is closed where it actually lives, in the UPDATE: `AND consolidated_at
IS NULL` means the second writer marks nothing and reports zero consolidated
sources, rather than silently stealing rows from the first summary.

THE GATES
---------
Every hop is conditional, and all four early exits are ordinary outcomes rather
than errors:

    no cluster            a small or diverse store has nothing to consolidate
    empty summary         the model returned nothing usable
    nothing after pii     defensive; redaction never drops a candidate today
    no embedding          `embed_candidates()` drops a candidate whose vector
                          came back missing or the wrong width, and an
                          unembedded summary is invisible to retrieval — a
                          write-only row. Better to write nothing.
"""

from __future__ import annotations

from typing import Any, Optional

from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from capture.embed import embed_node
from capture.pii import pii_node
from graphs.capture_state import Candidate
from jobs.reflection import (
    REFLECTION_SOURCE,
    select_cluster,
    summarize_cluster,
    write_summary,
)
from store.db import session

__all__ = [
    "ReflectionState",
    "build_reflection_graph",
    "get_reflection_graph",
    "run_reflection",
    "NODE_ORDER",
]

NODE_ORDER: tuple[str, ...] = ("select", "summarize", "pii", "embed", "write")

CONTINUE = "continue"
STOP = "end"


class ReflectionState(TypedDict, total=False):
    """State threaded through one reflection run for one subject."""

    # -- inputs --
    subject_id: str
    actor_id: str

    # -- select --
    cluster_ids: list[str]
    cluster_texts: list[str]
    seed_id: Optional[str]

    # -- summarize --
    summary_raw: str

    # -- pii (via capture.pii.pii_node) --
    summary_redacted: str
    pii_entities: list[str]

    # -- embed (via capture.embed.embed_node) --
    summary_embedding: Optional[list[float]]

    # -- write --
    summary_id: Optional[str]
    consolidated: list[str]

    # -- diagnostics --
    skipped: Optional[str]


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------

async def select_node(state: ReflectionState) -> dict[str, Any]:
    """Find the densest un-consolidated cluster. Own short transaction."""
    subject_id = state["subject_id"]
    actor_id = state["actor_id"]
    async with session(subject_id, actor_id) as conn:
        cluster = await select_cluster(conn, subject_id=subject_id)

    if cluster is None:
        return {"cluster_ids": [], "cluster_texts": [], "skipped": "no_cluster"}
    return {
        "cluster_ids": cluster.ids,
        "cluster_texts": cluster.texts,
        "seed_id": cluster.seed_id,
    }


async def summarize_node(state: ReflectionState) -> dict[str, Any]:
    """One completion over the cluster's texts."""
    from jobs.reflection import Cluster, ClusterMember

    cluster = Cluster(
        subject_id=state["subject_id"],
        seed_id=state.get("seed_id") or "",
        members=tuple(
            ClusterMember(memory_id, text, 0.0)
            for memory_id, text in zip(state["cluster_ids"], state["cluster_texts"])
        ),
    )
    text = await summarize_cluster(cluster)
    if not text:
        return {"summary_raw": "", "skipped": "empty_summary"}
    return {"summary_raw": text}


async def pii_adapter_node(state: ReflectionState) -> dict[str, Any]:
    """Run M2's `pii_node` over the summary sentence. See the module docstring."""
    candidate = Candidate(text=state["summary_raw"], source=REFLECTION_SOURCE)
    result = await pii_node(
        {"subject_id": state["subject_id"], "candidates": [candidate]}
    )
    redacted = result.get("redacted") or []
    if not redacted:  # pragma: no cover - redaction never drops today
        return {"summary_redacted": "", "skipped": "pii_dropped"}
    return {
        "summary_redacted": redacted[0].text,
        "pii_entities": list(redacted[0].pii_entities),
    }


async def embed_adapter_node(state: ReflectionState) -> dict[str, Any]:
    """Run M2's `embed_node` over the redacted summary. See the module docstring."""
    candidate = Candidate(text=state["summary_redacted"], source=REFLECTION_SOURCE)
    result = await embed_node({"subject_id": state["subject_id"], "scored": [candidate]})
    embedded = result.get("embedded") or []
    if not embedded or not embedded[0].embedding:
        return {"summary_embedding": None, "skipped": "no_embedding"}
    return {"summary_embedding": list(embedded[0].embedding)}


async def write_node(state: ReflectionState) -> dict[str, Any]:
    """Insert the summary, audit it, mark and audit its sources. One transaction."""
    subject_id = state["subject_id"]
    actor_id = state["actor_id"]
    async with session(subject_id, actor_id) as conn:
        result = await write_summary(
            conn,
            subject_id=subject_id,
            actor_id=actor_id,
            content=state["summary_redacted"],
            embedding=state.get("summary_embedding"),
            source_ids=state.get("cluster_ids") or [],
        )
    return {"summary_id": result["summary_id"], "consolidated": result["consolidated"]}


NODE_FUNCS = {
    "select": select_node,
    "summarize": summarize_node,
    "pii": pii_adapter_node,
    "embed": embed_adapter_node,
    "write": write_node,
}

#: The state key each node fills, and therefore the key its gate reads.
OUTPUT_KEY = {
    "select": "cluster_ids",
    "summarize": "summary_raw",
    "pii": "summary_redacted",
    "embed": "summary_embedding",
}


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------

def _gate(state_key: str):
    def decide(state: ReflectionState) -> str:
        return CONTINUE if state.get(state_key) else STOP

    decide.__name__ = f"gate_on_{state_key}"
    return decide


def build_reflection_graph() -> StateGraph:
    """Assemble (but do not compile) the reflection StateGraph."""
    builder = StateGraph(ReflectionState)
    for name in NODE_ORDER:
        builder.add_node(name, NODE_FUNCS[name])

    builder.set_entry_point(NODE_ORDER[0])
    for source, target in zip(NODE_ORDER, NODE_ORDER[1:]):
        builder.add_conditional_edges(
            source, _gate(OUTPUT_KEY[source]), {CONTINUE: target, STOP: END}
        )
    builder.add_edge(NODE_ORDER[-1], END)
    return builder


_compiled: Any = None


def get_reflection_graph() -> Any:
    global _compiled
    if _compiled is None:
        _compiled = build_reflection_graph().compile()
    return _compiled


async def run_reflection(*, subject_id: str, actor_id: str) -> ReflectionState:
    """Run one reflection pass for one subject and return the final state."""
    graph = get_reflection_graph()
    initial: ReflectionState = {
        "subject_id": str(subject_id),
        "actor_id": str(actor_id),
        "cluster_ids": [],
        "cluster_texts": [],
        "seed_id": None,
        "summary_raw": "",
        "summary_redacted": "",
        "pii_entities": [],
        "summary_embedding": None,
        "summary_id": None,
        "consolidated": [],
        "skipped": None,
    }
    return await graph.ainvoke(initial)
