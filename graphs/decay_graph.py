"""The decay graph (M8 step 5): claim -> compute -> apply -> archive -> stats.

One invocation of this graph is one **batch**, which is also one **transaction**.
The transaction is opened by the caller (`jobs/decay.py:run_decay_worker`) and
handed in on the state as `conn`; the graph never opens or commits anything.
That split is deliberate and worth stating, because the obvious alternative —
each node opening its own session — is broken here:

    the claim node takes `FOR UPDATE SKIP LOCKED` row locks, and those locks
    exist only for the life of the transaction that took them. If `apply` ran on
    a different connection, the locks would already have been released and two
    workers could be writing the same row. The whole concurrency guarantee lives
    in "claim and write share one transaction", so the transaction has to
    outlive the individual nodes.

WHY IT IS A GRAPH AT ALL
------------------------
Honest answer: this pipeline is linear, and a plain function would do the same
work. It is a LangGraph graph because the plan asks for one and because there is
a real benefit even so — each node writes its own state key, so a run is
inspectable after the fact (`claimed` / `updates` / `decayed_ids` /
`archived_ids`) in exactly the way `graphs/capture_state.py` describes, and the
short-circuit gate below means a drained table costs one query rather than five
nodes' worth of no-ops.

THE GATE
--------
`claim -> END` when the batch is empty. That is not an optimisation detail: the
drain loop calls this graph repeatedly and the LAST call in every worker's loop
is by definition the empty one, so on a three-worker run at least three of the
invocations claim nothing. Routing those straight to END keeps the empty case to
a single statement.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence

from langgraph.graph import END, StateGraph

# Same reasoning as `graphs/capture_state.py`: LangGraph hands the state schema
# to pydantic, which cannot introspect the stdlib TypedDict on Python < 3.12.
from typing_extensions import TypedDict

from jobs.claims import ClaimedRow, claim_batch
from jobs.decay import WeightUpdate, apply_weights, archive_rows, compute_updates

__all__ = [
    "DecayState",
    "build_decay_graph",
    "get_decay_graph",
    "run_decay_batch",
    "NODE_ORDER",
]

NODE_ORDER: tuple[str, ...] = ("claim", "compute", "apply", "archive", "stats")

CONTINUE = "continue"
STOP = "end"


class DecayState(TypedDict, total=False):
    """State threaded through one batch. Each node writes its own key."""

    # -- inputs (set by the caller) --
    conn: Any
    run_id: str
    batch_size: int
    subject_ids: Optional[Sequence[str]]
    now: Optional[datetime]

    # -- claim --
    claimed: list[ClaimedRow]
    claimed_ids: list[str]

    # -- compute --
    updates: list[WeightUpdate]

    # -- apply --
    decayed_ids: list[str]

    # -- archive --
    archive_candidates: list[str]
    archived_ids: list[str]

    # -- stats --
    run_stats: dict[str, Any]


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------

async def claim_node(state: DecayState) -> dict[str, Any]:
    """Take up to `batch_size` eligible rows and stamp them with this run id."""
    rows = await claim_batch(
        state["conn"],
        run_id=state["run_id"],
        batch_size=state["batch_size"],
        subject_ids=state.get("subject_ids"),
    )
    return {"claimed": rows, "claimed_ids": [r.id for r in rows]}


async def compute_node(state: DecayState) -> dict[str, Any]:
    """Pure arithmetic. No database access — see `jobs/decay.py:compute_updates`."""
    updates = compute_updates(state.get("claimed") or [], now=state.get("now"))
    return {"updates": updates}


async def apply_node(state: DecayState) -> dict[str, Any]:
    """Write every new weight in one statement."""
    updates = state.get("updates") or []
    decayed = await apply_weights(state["conn"], updates)
    return {"decayed_ids": decayed}


async def archive_node(state: DecayState) -> dict[str, Any]:
    """Stamp `archived_at` on the rows whose new weight fell below the threshold.

    Runs unconditionally rather than behind a gate: `archive_rows([])` is a
    no-op that does not touch the database, and a gate here would add a
    conditional edge whose only job is to skip a function that already returns
    early. The gate that matters is the one on `claim`.
    """
    candidates = [u.id for u in (state.get("updates") or []) if u.should_archive]
    archived = await archive_rows(state["conn"], candidates)
    return {"archive_candidates": candidates, "archived_ids": archived}


async def stats_node(state: DecayState) -> dict[str, Any]:
    """Record what this batch did (M8 step 12). Terminal node."""
    updates = state.get("updates") or []
    claimed = state.get("claimed_ids") or []
    decayed = state.get("decayed_ids") or []
    archived = state.get("archived_ids") or []
    return {
        "run_stats": {
            "run_id": state.get("run_id"),
            "claimed": len(claimed),
            "decayed": len(decayed),
            "archived": len(archived),
            "weight_delta": round(
                sum(u.new_weight - u.old_weight for u in updates), 6
            ),
            "min_new_weight": min((u.new_weight for u in updates), default=None),
            "max_new_weight": max((u.new_weight for u in updates), default=None),
        }
    }


NODE_FUNCS = {
    "claim": claim_node,
    "compute": compute_node,
    "apply": apply_node,
    "archive": archive_node,
    "stats": stats_node,
}


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------

def _gate_on_claim(state: DecayState) -> str:
    """Nothing claimed means the table is drained for this run: stop here."""
    return CONTINUE if state.get("claimed_ids") else STOP


def build_decay_graph() -> StateGraph:
    """Assemble (but do not compile) the decay StateGraph."""
    builder = StateGraph(DecayState)
    for name in NODE_ORDER:
        builder.add_node(name, NODE_FUNCS[name])

    builder.set_entry_point("claim")
    builder.add_conditional_edges(
        "claim", _gate_on_claim, {CONTINUE: "compute", STOP: END}
    )
    builder.add_edge("compute", "apply")
    builder.add_edge("apply", "archive")
    builder.add_edge("archive", "stats")
    builder.add_edge("stats", END)
    return builder


_compiled: Any = None


def get_decay_graph() -> Any:
    """Compile once and reuse. Compilation is pure structure — no I/O."""
    global _compiled
    if _compiled is None:
        _compiled = build_decay_graph().compile()
    return _compiled


async def run_decay_batch(
    conn: Any,
    *,
    run_id: str,
    batch_size: int,
    subject_ids: Sequence[str] | None = None,
    now: datetime | None = None,
) -> DecayState:
    """Run ONE batch on `conn`, which must already be inside a transaction."""
    graph = get_decay_graph()
    initial: DecayState = {
        "conn": conn,
        "run_id": str(run_id),
        "batch_size": int(batch_size),
        "subject_ids": list(subject_ids) if subject_ids is not None else None,
        "now": now,
        "claimed": [],
        "claimed_ids": [],
        "updates": [],
        "decayed_ids": [],
        "archive_candidates": [],
        "archived_ids": [],
        "run_stats": {},
    }
    return await graph.ainvoke(initial)
