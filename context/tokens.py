"""Token counting for the budget (plan step 6).

One function matters here: `count_tokens(text)`. The composer calls it on the
*whole rendered block* on every iteration of the drop loop, so it has to be
cheap and it has to be consistent — a counter that disagreed with itself between
two calls would make the loop non-terminating in the worst case.

MODEL RESOLUTION — WHY THIS IS NOT `tiktoken.encoding_for_model(...)`
---------------------------------------------------------------------
The model name is **sourced from the environment**, through
`llm.config.resolve_completion_model()`, which reads `LLM_MODEL` on every call.
No model string is written down in this file, per M1's rule that `llm/config.py`
is the only place one may appear (and `test_no_model_literals_outside_llm_config`
enforces it).

Handing that name straight to tiktoken does not work. What `LLM_MODEL` holds is
a LiteLLM *route* — provider prefix, then vendor, then model, slash-separated —
and tiktoken only knows bare vendor model names, so it raises:

    KeyError: Could not automatically map <route> to a tokeniser.

So `encoding_for()` peels the prefixes off one segment at a time, longest first,
and asks tiktoken about each suffix. If none of them resolve it falls back to the
encoding named by `CONTEXT_FALLBACK_ENCODING` (see `context/config.py`), which
defaults to the byte-pair encoding this project's completion family uses.

HONESTY ABOUT THE COUNT
-----------------------
Three modes, and exactly what each one guarantees:

1. **Exact.** The route resolved to the served model's own encoding. Counts are
   the real thing. This is the live configuration today.

2. **Fallback encoding.** The route did not resolve, so the count comes from the
   encoding named by `CONTEXT_FALLBACK_ENCODING`. Measured against the
   currently-configured model's encoding this agrees exactly on every sample
   tried, including CJK and emoji — the two are the same BPE family. That
   equality is **scoped to that family**: point `LLM_MODEL` at a model with a
   different tokenizer (a SentencePiece-based one, say) and this mode becomes an
   approximation with no guaranteed direction. It is not a safe mode for an
   arbitrary model, only a good one for this one.

3. **No tokenizer at all.** `tiktoken` fetches its BPE vocabulary over the
   network on first use of an encoding; with a cold cache and no network that
   raises. A token *counter* failing must not take down a chat reply, so
   `count_tokens` degrades to an estimate and logs it once.

WHY THE ESTIMATE COUNTS BYTES, NOT CHARACTERS
---------------------------------------------
Mode 3 must never under-count: under-counting emits an over-budget block, which
is the precise failure plan steps 8-10 exist to prevent, and the composer's
`AssertionError` guard cannot catch it because that guard re-uses this very
counter. Over-counting only costs a dropped memory.

A chars-per-token divisor cannot give that. The first version of this file used
3.0 chars/token, reasoning from English prose at ~4 — and it under-counted on
more than half of a 21-sample sweep, badly:

    Japanese         19 real tokens vs  9 estimated
    emoji            20 real         vs  7
    flag emoji       12 real         vs  2
    Hebrew            6 real         vs  3
    accented Latin   13 real         vs  7
    punctuation ASCII 70 real        vs 34

Composed live, Japanese content against a 120-token budget produced a block the
estimate called 92 tokens and the real tokenizer called 192 — 60% over budget.
The cause is structural, not a bad constant: `len(str)` counts Unicode code
points, and a BPE trained mostly on English spends many tokens per code point
outside its training distribution. No single divisor is safe for all scripts,
and `/2` is not either — the punctuation-dense sample above runs at roughly 1.46
chars/token.

The bound that actually holds is bytes. Every BPE token decodes to **at least
one byte** of the input, and the tokens' bytes concatenate back to the input
exactly, so

    token_count <= len(text.encode("utf-8"))

for any text, any script, any BPE encoding. That is a theorem about the encoding
scheme rather than a measurement, which is why it is what this module uses.

The price is honesty about cost: on English prose (~4 bytes/token) it over-counts
about fourfold, so a degraded process fits roughly a quarter of the memories it
otherwise would. That is the correct trade — a smaller memory block, never an
over-budget prompt — and it only applies when there is no tokenizer at all.

So: the budget is enforced against whichever counter is live, and in modes 1 and
3 that counter provably never under-counts, so the composer's guarantee — the
block fits the budget it was given — holds. In mode 2 it holds for the current
model family and is an approximation outside it.
"""

from __future__ import annotations

import logging

from context import config

logger = logging.getLogger(__name__)

__all__ = [
    "count_tokens",
    "encoding_for",
    "encoding_name_for_model",
    "estimate_tokens",
    "is_exact",
]

_ENCODING_CACHE: dict[str, object] = {}
_WARNED = False


def _resolve_model() -> str:
    """The completion model name, via the single LLM seam. Never hardcoded."""
    from llm.config import resolve_completion_model

    return resolve_completion_model()


def encoding_name_for_model(model: str) -> str | None:
    """The tiktoken encoding for `model`, or `None` if tiktoken cannot map it.

    Tries the full name first, then each suffix after a `/`, so LiteLLM's
    provider-prefixed routes resolve to the underlying model's tokenizer.
    """
    try:
        import tiktoken
    except ImportError:  # pragma: no cover - tiktoken is a declared dependency
        return None

    parts = model.split("/")
    for start in range(len(parts)):
        candidate = "/".join(parts[start:])
        if not candidate:
            continue
        try:
            return tiktoken.encoding_for_model(candidate).name
        except (KeyError, ValueError):
            continue
    return None


def encoding_for(model: str | None = None):
    """A cached tiktoken encoding for `model`, or `None` if none can be loaded.

    Cached because building an encoding parses a multi-megabyte BPE table, and
    the composer's drop loop calls the counter once per iteration.
    """
    resolved = model or _resolve_model()
    if resolved in _ENCODING_CACHE:
        return _ENCODING_CACHE[resolved]

    try:
        import tiktoken
    except ImportError:  # pragma: no cover - declared dependency
        _ENCODING_CACHE[resolved] = None
        return None

    name = encoding_name_for_model(resolved) or config.fallback_encoding()
    try:
        encoding = tiktoken.get_encoding(name)
    except Exception as exc:  # noqa: BLE001 - network/vocab failures must not raise here
        logger.warning(
            "tiktoken could not load encoding %r for model %r (%s: %s); "
            "falling back to a character-based token estimate",
            name,
            resolved,
            type(exc).__name__,
            exc,
        )
        encoding = None

    _ENCODING_CACHE[resolved] = encoding
    return encoding


def is_exact(model: str | None = None) -> bool:
    """True when counts come from a real tokenizer rather than the estimate."""
    return encoding_for(model) is not None


def estimate_tokens(text: str) -> int:
    """A tokenizer-free UPPER BOUND on the token count of `text`.

    Returns the UTF-8 byte length. Every BPE token decodes to at least one byte
    and the tokens' bytes reassemble the input exactly, so the real count can
    never exceed this — for any script, any encoding. See the module docstring
    for why the chars-per-token divisor this replaced was unsound (it
    under-counted Japanese, emoji, Hebrew, accented Latin and punctuation-dense
    ASCII, in one case by a factor of six).

    Loose on English prose by roughly 4x. That is the intended direction: the
    only alternative is a bound that sometimes fails, and a token budget that
    sometimes fails is not a budget.
    """
    if not text:
        return 0
    return len(text.encode("utf-8"))


def count_tokens(text: str, model: str | None = None) -> int:
    """Tokens in `text` for the configured model. Always a non-negative int.

    Total by construction: `None`, non-strings and an unavailable tokenizer all
    produce a number rather than an exception. When no tokenizer can be loaded
    the return value is an upper bound rather than an estimate — see
    `estimate_tokens`.
    """
    global _WARNED

    if text is None:
        return 0
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return 0

    encoding = encoding_for(model)
    if encoding is not None:
        return len(encoding.encode(text, disallowed_special=()))

    if not _WARNED:
        logger.warning(
            "no tokenizer available; falling back to a UTF-8 byte-length upper "
            "bound on token counts. It never under-counts, but it over-counts "
            "English prose roughly fourfold, so the memory block will be short."
        )
        _WARNED = True
    return estimate_tokens(text)
