"""Node 3 -- score importance/confidence and apply the confidence floor (plan step 4).

Two responsibilities, deliberately kept in two separate functions:

``score_candidates()``   assigns `importance` and `confidence` in 0..1. One
                         batched LLM call for the whole list, with a
                         deterministic heuristic fallback so a provider hiccup
                         degrades scoring rather than losing the turn.

``apply_confidence_floor()``  a pure filter. No I/O, no model.

The split is what makes `test_low_confidence_candidate_is_dropped` a real test:
it can stub the scorer and still exercise the genuine floor logic and the
genuine graph short-circuit, instead of testing a mock end to end.
"""

from __future__ import annotations

import json
import re
from typing import Any

from llm import config as llm_config

from capture import config as capture_config
from capture.metrics import log_warning, node_span
from graphs.capture_state import Candidate, CaptureState

SYSTEM_PROMPT = """You score extracted facts about a user for a long-term memory store.

Input: a JSON array of fact strings.
Output: ONLY a JSON array of the same length and order. No prose, no fences.

Each element:
  {"importance": <float 0..1>, "confidence": <float 0..1>}

importance -- how much knowing this would improve future replies to this user.
  0.9-1.0  identity, health, safety, allergies, accessibility needs
  0.6-0.8  stable preferences, relationships, job, location, ongoing projects
  0.3-0.5  mild preferences, minor habits
  0.0-0.2  trivia, near-certainly irrelevant later

confidence -- how sure you are the fact is (a) actually stated and (b) durable.
  0.9-1.0  stated plainly and unambiguously by the user about themselves
  0.6-0.8  clearly implied
  0.3-0.5  hedged, second-hand, or possibly transient
  0.0-0.2  speculative or likely an inference the turn does not support
"""

# Words that mark a hedged claim; used only by the offline fallback scorer.
_HEDGES = ("maybe", "might", "possibly", "perhaps", "i think", "not sure", "probably", "seems")
_HIGH_VALUE = (
    "allerg", "diabet", "medic", "health", "safety", "disab", "name is",
    "lives in", "works as", "job", "birthday", "married", "child", "daughter",
    "son", "wife", "husband", "partner", "prefer", "favorite", "favourite",
)


def _clamp01(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(0.0, min(1.0, number))


def heuristic_scores(candidate: Candidate) -> tuple[float, float]:
    """Deterministic, offline fallback. Returns `(importance, confidence)`.

    Intentionally conservative rather than clever: it exists so a provider
    outage degrades to *plausible* scores, not so it replaces the model.
    """
    text = candidate.text.lower()
    importance = 0.75 if any(token in text for token in _HIGH_VALUE) else 0.5
    confidence = 0.5 if any(hedge in text for hedge in _HEDGES) else 0.7
    if candidate.source == "assistant_note":
        confidence -= 0.1
    return round(importance, 3), round(max(0.0, confidence), 3)


def parse_scores(raw: str, expected: int) -> list[dict[str, float]] | None:
    """Parse the scorer's JSON array. Returns None if it cannot be trusted."""
    if not raw or not raw.strip():
        return None
    fenced = re.match(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", raw, re.DOTALL)
    body = fenced.group(1) if fenced else raw
    start, end = body.find("["), body.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list) or len(parsed) != expected:
        # A length mismatch means we cannot map scores back to candidates by
        # position. Better to fall back wholesale than to misattribute scores.
        return None
    return [item if isinstance(item, dict) else {} for item in parsed]


async def score_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Attach `importance` and `confidence` to every candidate. One LLM call."""
    if not candidates:
        return []

    payload = json.dumps([c.text for c in candidates], ensure_ascii=False)
    scores: list[dict[str, float]] | None = None
    try:
        raw = await llm_config.complete(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Facts:\n{payload}\n\nJSON array:"},
            ],
            temperature=0.0,
        )
        scores = parse_scores(raw, len(candidates))
        if scores is None:
            log_warning("capture.evaluate.unparseable", raw=(raw or "")[:200])
    except Exception as exc:
        log_warning("capture.evaluate.provider_error", error=f"{type(exc).__name__}: {exc}")

    out: list[Candidate] = []
    for index, candidate in enumerate(candidates):
        fallback_importance, fallback_confidence = heuristic_scores(candidate)
        entry = scores[index] if scores is not None else {}
        out.append(
            candidate.with_(
                importance=_clamp01(entry.get("importance"), fallback_importance),
                confidence=_clamp01(entry.get("confidence"), fallback_confidence),
            )
        )
    return out


def apply_confidence_floor(
    candidates: list[Candidate], floor: float | None = None
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """Split candidates into `(kept, dropped_records)` on the confidence floor.

    Pure. `>=` is intentional: a candidate scoring exactly the floor is kept,
    so setting the floor to 0.0 keeps everything.
    """
    limit = floor if floor is not None else capture_config.confidence_floor()
    kept: list[Candidate] = []
    dropped: list[dict[str, Any]] = []
    for candidate in candidates:
        confidence = candidate.confidence if candidate.confidence is not None else 0.0
        if confidence >= limit:
            kept.append(candidate)
        else:
            dropped.append(
                {
                    "node": "evaluate",
                    "reason": "below_confidence_floor",
                    "floor": limit,
                    "confidence": confidence,
                    "text": candidate.text,
                }
            )
    return kept, dropped


async def evaluate_node(state: CaptureState) -> dict[str, Any]:
    """LangGraph node: state['redacted'] -> state['scored']."""
    subject_id = state.get("subject_id", "")
    candidates = state.get("redacted") or []

    with node_span("evaluate", subject_id, n_in=len(candidates)) as span:
        scored = await score_candidates(candidates)
        kept, dropped = apply_confidence_floor(scored)
        span["out"] = len(kept)
        span["floor"] = capture_config.confidence_floor()
        span["dropped_texts"] = [d["text"] for d in dropped]

    return {"scored": kept, "dropped": list(state.get("dropped") or []) + dropped}
