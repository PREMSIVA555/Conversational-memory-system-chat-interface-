"""Node 1 -- extract atomic candidate facts from a conversation turn (plan step 2).

The contract that matters here is the *empty* one: most chat turns contain
nothing worth remembering, and the node must return `[]` cleanly rather than
inventing a fact to justify its own existence. Two mechanisms enforce that:

1. a prompt that names "return an empty array" as the expected answer for
   greetings, thanks, small talk and questions, with worked examples; and
2. `_coerce_candidates`, which treats any unparseable or non-list model output
   as "nothing memorable" instead of raising -- a malformed extraction must
   never take down a capture job, and it must never guess.

The node is split into a pure-ish `extract_candidates()` (callable and testable
on its own) and the thin `extract_node()` LangGraph wrapper.
"""

from __future__ import annotations

import json
import re
from typing import Any

from llm import config as llm_config

from capture import config as capture_config
from capture.metrics import log_event, log_warning, node_span
from graphs.capture_state import Candidate, CaptureState, Turn, turn_text

# Free-form source tags the model may pick from. Anything else it invents is
# accepted too (the column is plain text), but a closed list keeps the values
# consistent enough to aggregate on later.
SOURCE_TAGS = (
    "user_statement",   # the user asserted it about themselves
    "user_preference",  # a like/dislike/habit
    "user_plan",        # something intended or scheduled
    "assistant_note",   # the assistant established it during the turn
)

SYSTEM_PROMPT = """You extract durable, atomic facts about the USER from one turn of a conversation.

Return ONLY a JSON array. No prose, no markdown fences, no explanation.

Each array element is an object:
  {"text": "<one self-contained fact, third person, about the user>",
   "source": "<one of: user_statement, user_preference, user_plan, assistant_note>"}

RULES
- Atomic: one fact per element. Split compound statements.
- Self-contained: "The user's sister is named Mia", not "her sister is Mia".
- Durable: only things still true next week. Skip transient state.
- Grounded: only what the turn actually says. Never infer or embellish.
- Preserve identifiers verbatim if the user states them; do not paraphrase numbers.

RETURN AN EMPTY ARRAY [] when the turn holds nothing durable about the user.
That is the correct, expected answer for greetings, thanks, acknowledgements,
small talk, pure questions, and requests for help.

EXAMPLES
Turn: "User: hi"                                     -> []
Turn: "User: thanks!"                                -> []
Turn: "User: what's the weather?"                    -> []
Turn: "User: Can you explain recursion?"             -> []
Turn: "User: I'm allergic to peanuts."               -> [{"text": "The user is allergic to peanuts.", "source": "user_statement"}]
Turn: "User: I moved to Lisbon and I work as a nurse." -> [{"text": "The user lives in Lisbon.", "source": "user_statement"}, {"text": "The user works as a nurse.", "source": "user_statement"}]
"""


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` wrappers some models add despite instructions."""
    fenced = re.match(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", text, re.DOTALL)
    return fenced.group(1) if fenced else text


def _first_json_array(text: str) -> str | None:
    """Return the outermost bracketed span, so leading chatter is tolerated."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    return text[start : end + 1]


def parse_extraction(raw: str) -> list[Candidate]:
    """Turn raw model output into candidates. Never raises; unparseable -> []."""
    if not raw or not raw.strip():
        return []

    body = _first_json_array(_strip_fences(raw))
    if body is None:
        log_warning("capture.extract.unparseable", reason="no_json_array", raw=raw[:200])
        return []

    try:
        parsed: Any = json.loads(body)
    except json.JSONDecodeError as exc:
        log_warning("capture.extract.unparseable", reason=str(exc), raw=body[:200])
        return []

    if not isinstance(parsed, list):
        log_warning("capture.extract.unparseable", reason="not_a_list", raw=body[:200])
        return []

    return _coerce_candidates(parsed)


def _coerce_candidates(items: list[Any]) -> list[Candidate]:
    """Accept both the documented object shape and a bare list of strings."""
    out: list[Candidate] = []
    for item in items:
        if isinstance(item, str):
            text, source = item, "user_statement"
        elif isinstance(item, dict):
            text = item.get("text") or item.get("fact") or item.get("content") or ""
            source = item.get("source") or "user_statement"
        else:
            continue

        text = str(text).strip()
        if not text:
            continue
        source = str(source).strip() or "user_statement"
        out.append(Candidate(text=text, source=source))
    return out


#: Log event emitted when a turn is skipped without consulting the model.
PRE_FILTER_EVENT = "capture.extract.pre_filtered"


def pre_filter_reason(text: str) -> str | None:
    """Why this turn can be skipped without a provider call, or None to ask the model.

    Deliberately narrow: it fires ONLY on a turn with no text at all. Deciding
    that "hi" or "thanks" is non-memorable is the *model's* job, not a keyword
    list's -- a stoplist here would make
    `test_extract_returns_empty_for_nonmemorable_turn` pass without the
    extractor ever being exercised, and would risk swallowing short but highly
    memorable turns ("I'm coeliac", "I'm allergic to peanuts"), which are
    exactly the facts worth remembering. `test_pre_filter_only_fires_on_empty_turns`
    pins that boundary.

    WHY IT RETURNS A REASON RATHER THAN A BOOL -- an empty candidate list now has
    three possible causes, and they must stay distinguishable:

      1. pre-filtered trivial turn  -> healthy, 0 provider calls, logs this event
      2. model ran, found nothing   -> healthy, 1 provider call, no error logged
      3. provider failed            -> BROKEN,  0 provider calls, logs provider_error

    A bare bool would leave (1) and (3) looking identical from the outside,
    which is the ambiguity the call-count guard exists to remove. Emitting a
    distinct event means each case has its own positive signal.
    """
    if not text.strip():
        return "empty_turn"
    return None


async def extract_candidates(turn: Turn, *, max_candidates: int | None = None) -> list[Candidate]:
    """Extract zero or more atomic facts from `turn`."""
    text = turn_text(turn)
    skipped = pre_filter_reason(text)
    if skipped is not None:
        # A positive signal, so "skipped a trivial turn" is never mistaken for
        # "the provider died and the error was swallowed".
        log_event(PRE_FILTER_EVENT, reason=skipped)
        return []

    cap = max_candidates if max_candidates is not None else capture_config.max_candidates_per_turn()

    try:
        raw = await llm_config.complete(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Turn:\n{text}\n\nJSON array:"},
            ],
            temperature=0.0,
        )
    except Exception as exc:
        # A provider failure must not lose the job silently, but it also must
        # not fabricate memories. Surface it and capture nothing this turn.
        log_warning("capture.extract.provider_error", error=f"{type(exc).__name__}: {exc}")
        return []

    candidates = parse_extraction(raw)
    if len(candidates) > cap:
        log_warning("capture.extract.capped", proposed=len(candidates), cap=cap)
        candidates = candidates[:cap]
    return candidates


async def extract_node(state: CaptureState) -> dict[str, Any]:
    """LangGraph node: turn -> state['candidates']."""
    subject_id = state.get("subject_id", "")
    turn = state.get("turn", {})
    with node_span("extract", subject_id, n_in=1) as span:
        candidates = await extract_candidates(turn)
        span["out"] = len(candidates)
        span["texts"] = [c.text for c in candidates]
    return {"candidates": candidates}
