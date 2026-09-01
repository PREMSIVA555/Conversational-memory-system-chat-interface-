"""Response graph state (plan step 7).

The graph threads one `ResponseState` dict through two nodes, `retrieve` then
`respond`. Like `graphs/capture_state.py`, each node writes its own keys, so the
final state is a record of what happened rather than a bag that gets overwritten:
after a run you can read `memory_block` (what the model was shown), `memory_ids`
(which rows that came from) and `degraded` (whether the memory layer was skipped)
and reconstruct the turn exactly.

`degraded` and `memory_ids` are the two fields plan step 12 puts on the wire, and
the two M7's audit log will persist. They mean different things and both are
needed:

    degraded=False, memory_ids=[]     the memory layer worked; this user simply
                                      has nothing relevant stored yet
    degraded=True,  memory_ids=[]     the memory layer was unavailable; there
                                      may well have been relevant memories and
                                      the model never saw them

An empty candidate list alone cannot tell those apart, and an audit trail that
conflates them is worse than none — it would claim the assistant had no memories
to draw on when in fact it was blindfolded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

# NOT `typing.TypedDict` — see `graphs/capture_state.py` for the full reason:
# LangGraph hands the schema to pydantic, which cannot introspect the stdlib
# TypedDict on Python < 3.12 and raises at graph-compile time.
from typing_extensions import TypedDict

__all__ = [
    "ResponseState",
    "ResponseMetadata",
    "Message",
    "new_state",
    "latest_user_message",
]

#: One conversation turn, in the shape `llm/config.py:complete()` accepts.
Message = dict[str, str]

#: What a streaming consumer receives, and what the graph pushes into it. An
#: async callable taking one event dict. `None` means "collect, do not stream" —
#: the non-streaming path uses exactly the same nodes with this left unset.
Emitter = Callable[[dict[str, Any]], Awaitable[None]]


class ResponseState(TypedDict, total=False):
    """State for one chat turn passing through the response graph."""

    # --- inputs -----------------------------------------------------------
    subject_id: str
    actor_id: str
    messages: list[Message]

    # --- written by the retrieval node ------------------------------------
    memory_block: str
    memory_ids: list[str]
    degraded: bool
    degraded_reason: Optional[str]
    retrieval_ms: float

    # --- written by the response node -------------------------------------
    reply: str
    prompt: list[Message]

    # --- transport seam ---------------------------------------------------
    emit: Optional[Any]
    """Async event sink, or None. Typed `Any` rather than `Emitter` because
    pydantic — which LangGraph builds a validator from — would otherwise try to
    validate a callable against a `Callable[...]` annotation it cannot construct.
    `Any` passes it through untouched, which is all the graph needs."""


@dataclass(frozen=True, slots=True)
class ResponseMetadata:
    """The leading metadata event / response header payload (plan step 12).

    Emitted by the retrieval node **before** the response node produces a single
    token, so a consumer knows what context the answer was built from before it
    starts rendering the answer.
    """

    degraded: bool = False
    memory_ids: list[str] = field(default_factory=list)
    degraded_reason: Optional[str] = None
    memory_tokens: int = 0

    def to_event(self) -> dict[str, Any]:
        return {
            "type": "metadata",
            "degraded": self.degraded,
            "memory_ids": list(self.memory_ids),
            "memory_count": len(self.memory_ids),
            "degraded_reason": self.degraded_reason,
            "memory_tokens": self.memory_tokens,
        }


def new_state(
    subject_id: str,
    actor_id: str,
    messages: list[Message],
    *,
    emit: Any | None = None,
) -> ResponseState:
    """Build the initial state with every downstream key pre-seeded.

    Pre-seeding matters: a consumer reading `state["degraded"]` must never get a
    `KeyError` because the retrieval node short-circuited, and `False` is the
    honest default for a turn that has not tried to retrieve yet.
    """
    return ResponseState(
        subject_id=subject_id,
        actor_id=actor_id,
        messages=list(messages),
        memory_block="",
        memory_ids=[],
        degraded=False,
        degraded_reason=None,
        retrieval_ms=0.0,
        reply="",
        prompt=[],
        emit=emit,
    )


def latest_user_message(messages: list[Message]) -> str:
    """The text the retrieval query is built from: the last user turn.

    The last *user* turn specifically, not the last turn of any role — an
    assistant message is the system talking to itself, and retrieving against it
    would search memory for the assistant's own phrasing rather than for what
    the person actually asked.
    """
    for message in reversed(messages or []):
        if (message.get("role") or "").lower() == "user":
            return message.get("content") or ""
    return ""
