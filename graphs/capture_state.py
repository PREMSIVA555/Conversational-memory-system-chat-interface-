"""Capture graph state (plan step 1).

The graph threads one ``CaptureState`` dict through six nodes. Each node writes
its **own** key rather than mutating a shared list, so the state doubles as an
audit trail of the pipeline: after a run you can read `candidates` (what the
extractor proposed), `redacted` (what survived PII scrubbing), `scored` (what
cleared the confidence floor), `embedded` (what got vectors and dedup verdicts)
and `write_results` (what actually hit the database) and see exactly where a
fact was dropped.

`Candidate` is a frozen-ish dataclass carried through all of those keys. Nodes
never mutate a candidate in place -- they `dataclasses.replace()` it into the
next key -- so an earlier key always still shows the earlier state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal, Optional

# NOT `typing.TypedDict`. LangGraph hands the state schema to pydantic to build
# a validator, and on Python < 3.12 pydantic cannot introspect the stdlib
# TypedDict -- it raises PydanticUserError at graph-compile time. The
# typing_extensions backport carries the field metadata pydantic needs.
from typing_extensions import TypedDict

DedupStatus = Literal["new", "duplicate"]


@dataclass
class Candidate:
    """One atomic fact on its way to becoming a `memories` row."""

    # --- from extract ---
    text: str
    source: str = "chat"

    # --- from pii ---
    pii_entities: list[str] = field(default_factory=list)
    raw_text: Optional[str] = None
    """The pre-redaction text. Kept in memory for logging/tests only -- it is
    never passed to `store/memories.py` and never reaches the `content` column."""

    # --- from evaluate ---
    importance: Optional[float] = None
    confidence: Optional[float] = None

    # --- from embed ---
    embedding: Optional[list[float]] = None

    # --- from dedup ---
    dedup_status: Optional[DedupStatus] = None
    duplicate_of: Optional[str] = None
    similarity: Optional[float] = None

    def with_(self, **changes: Any) -> "Candidate":
        """Return a copy with `changes` applied. Never mutates the original."""
        return replace(self, **changes)

    def summary(self) -> dict[str, Any]:
        """Log-safe projection: no raw text, no 1024-float vector."""
        return {
            "text": self.text,
            "source": self.source,
            "pii_entities": list(self.pii_entities),
            "importance": self.importance,
            "confidence": self.confidence,
            "has_embedding": self.embedding is not None,
            "dedup_status": self.dedup_status,
            "duplicate_of": self.duplicate_of,
            "similarity": self.similarity,
        }

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Turn(TypedDict, total=False):
    """One conversational exchange handed to the capture graph."""

    user: str
    assistant: str


class WriteResult(TypedDict, total=False):
    """What the write node actually did with one candidate."""

    text: str
    action: Literal["insert", "reinforce"]
    memory_id: str
    similarity: Optional[float]
    dedup_status_at_write: str
    dedup_status_from_node: Optional[str]


class CaptureState(TypedDict, total=False):
    """The dict LangGraph threads through extract -> ... -> write.

    `total=False` because each node contributes only its own keys; LangGraph
    merges partial dicts returned by nodes into the running state.
    """

    subject_id: str
    actor_id: str
    turn: Turn

    candidates: list[Candidate]     # extract
    redacted: list[Candidate]       # pii
    scored: list[Candidate]         # evaluate
    embedded: list[Candidate]       # embed, then annotated in place by dedup
    write_results: list[WriteResult]  # write

    # diagnostics
    dropped: list[dict[str, Any]]
    error: Optional[str]


def new_state(subject_id: str, actor_id: str, turn: Turn) -> CaptureState:
    """Build a fresh state with every collection key initialised to empty.

    Pre-seeding the lists matters: a run that short-circuits to END at the
    extract node still returns `write_results == []` rather than a missing key,
    so callers never have to write `state.get("write_results", [])`.
    """
    return CaptureState(
        subject_id=str(subject_id),
        actor_id=str(actor_id),
        turn=turn,
        candidates=[],
        redacted=[],
        scored=[],
        embedded=[],
        write_results=[],
        dropped=[],
        error=None,
    )


def turn_text(turn: Turn) -> str:
    """Flatten a turn into the text the extractor reads."""
    user = (turn.get("user") or "").strip()
    assistant = (turn.get("assistant") or "").strip()
    parts = []
    if user:
        parts.append(f"User: {user}")
    if assistant:
        parts.append(f"Assistant: {assistant}")
    return "\n".join(parts)
