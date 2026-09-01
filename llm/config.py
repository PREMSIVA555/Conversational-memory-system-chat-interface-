"""The single LLM seam for the whole codebase.

Plan step 14 is strict: **no model name is hardcoded anywhere else**. Every
completion and every embedding in memory-system goes through ``complete()`` and
``embed()`` below, and both resolve their model from the environment on every
call — so monkeypatching ``LLM_MODEL`` changes behaviour immediately and no
caller can pin a model behind this module's back.

Two providers sit behind the seam (see harness.md D1/D2):

  completions  ``groq/openai/gpt-oss-120b``  — Groq
  embeddings   ``voyage/voyage-3.5``         — Voyage AI, 1024 dims

Groq exposes no embeddings endpoint, hence the split. LiteLLM routes both from
their prefix (``groq/``, ``voyage/``) plus ``GROQ_API_KEY`` / ``VOYAGE_API_KEY``,
so the two-provider split is invisible to every caller.

MAX_TOKENS TRAP — read before lowering the default
--------------------------------------------------
gpt-oss models spend their token budget on internal reasoning *before* emitting
any content. Call one with a small ``max_tokens`` and ``content`` comes back an
empty string with ``finish_reason='stop'`` — it looks like a broken model, but
it is a truncated reasoning phase. ``DEFAULT_MAX_TOKENS`` is therefore floored
at 512 and ``complete()`` refuses to go below ``MIN_MAX_TOKENS``. Do not "fix"
an empty completion by switching models.

RATE LIMIT TRAP — why there is a retry loop here
------------------------------------------------
The Voyage key is on the no-payment tier: **3 requests per minute**, metered per
request rather than per token. LiteLLM's own ``num_retries`` is not enough — it
does not honour ``Retry-After`` and its backoff is far too short for a per-minute
window, so a 429 propagates to the caller and whatever was being embedded is
simply lost.

That made M1's embedding test flaky the moment a second agent embedded in
parallel: it failed under concurrency and passed on every isolated re-run, which
is the signature of a shared quota rather than a bug in the test.

``_with_rate_limit_retry`` below wraps both calls with jittered exponential
backoff that prefers the provider's own ``Retry-After`` when present. It turns a
hard failure into a slow success, which is the right trade for a background
capture path. Callers that genuinely cannot wait should pass a smaller
``rate_limit_attempts``.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Sequence, TypeVar

_ENV_PATH = Path(__file__).resolve().parent.parent / "infra" / ".env"
_env_loaded = False

# The floor below which gpt-oss reliably returns empty content. See module docstring.
MIN_MAX_TOKENS = 512

DEFAULTS = {
    "LLM_MODEL": "groq/openai/gpt-oss-120b",
    "LLM_FALLBACK_MODEL": "groq/qwen/qwen3.8-27b",
    "EMBEDDING_MODEL": "voyage/voyage-3.5",
    "EMBEDDING_DIM": "1024",
    "LLM_MAX_TOKENS": "1024",
    "LLM_TIMEOUT_SECONDS": "60",
    "LLM_MAX_RETRIES": "2",
    # 429 handling — see RATE LIMIT TRAP in the module docstring.
    "LLM_RATE_LIMIT_ATTEMPTS": "6",
    "LLM_RATE_LIMIT_BASE_DELAY": "4.0",
    "LLM_RATE_LIMIT_MAX_DELAY": "60.0",
}


def load_env(override: bool = False) -> None:
    """Load ``infra/.env`` once. Real environment variables win by default."""
    global _env_loaded
    if _env_loaded and not override:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        _env_loaded = True
        return
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH, override=override)
    _env_loaded = True


def _env(name: str) -> str:
    load_env()
    value = os.environ.get(name)
    if value is None or value == "":
        return DEFAULTS[name]
    return value


# ---------------------------------------------------------------------------
# resolution — every one of these reads env at call time, never at import time
# ---------------------------------------------------------------------------

def resolve_completion_model() -> str:
    """The model ``complete()`` will use. Reads ``LLM_MODEL`` on every call."""
    return _env("LLM_MODEL")


def resolve_fallback_model() -> str:
    return _env("LLM_FALLBACK_MODEL")


def resolve_embedding_model() -> str:
    """The model ``embed()`` will use. Reads ``EMBEDDING_MODEL`` on every call."""
    return _env("EMBEDDING_MODEL")


def resolve_embedding_dim() -> int:
    """Vector width. Also templated into the memories.embedding column."""
    return int(_env("EMBEDDING_DIM"))


def default_max_tokens() -> int:
    return max(MIN_MAX_TOKENS, int(_env("LLM_MAX_TOKENS")))


def request_timeout() -> float:
    return float(_env("LLM_TIMEOUT_SECONDS"))


def max_retries() -> int:
    return int(_env("LLM_MAX_RETRIES"))


def rate_limit_attempts() -> int:
    return int(_env("LLM_RATE_LIMIT_ATTEMPTS"))


# ---------------------------------------------------------------------------
# 429 handling
# ---------------------------------------------------------------------------

T = TypeVar("T")

_RETRY_AFTER_RE = re.compile(r"retry[- _]?after[\"'\s:=]+([0-9]+(?:\.[0-9]+)?)", re.I)


def _is_rate_limit(exc: BaseException) -> bool:
    """True when ``exc`` is a provider 429.

    Matched structurally where possible (LiteLLM raises ``RateLimitError`` and
    sets ``status_code``); the string check is a fallback for wrapped errors,
    since both providers surface 429 differently.
    """
    if type(exc).__name__ in {"RateLimitError", "APIStatusError"}:
        return True
    if getattr(exc, "status_code", None) == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "rate_limit" in text


def _retry_after_seconds(exc: BaseException) -> float | None:
    """The provider's own requested wait, if it gave one. Always beats our guess."""
    value = getattr(exc, "retry_after", None)
    if value is None:
        headers = getattr(exc, "response_headers", None) or getattr(exc, "headers", None)
        if isinstance(headers, dict):
            value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        match = _RETRY_AFTER_RE.search(str(exc))
        value = match.group(1) if match else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def _with_rate_limit_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int | None = None,
    what: str = "llm call",
) -> T:
    """Run ``operation``, retrying only on 429 with jittered exponential backoff.

    Deliberately narrow: any error that is not a rate limit is re-raised on the
    first occurrence. Retrying a malformed request or a bad key just wastes a
    minute and hides the real cause.
    """
    total = attempts if attempts is not None else rate_limit_attempts()
    total = max(1, total)
    base = float(_env("LLM_RATE_LIMIT_BASE_DELAY"))
    ceiling = float(_env("LLM_RATE_LIMIT_MAX_DELAY"))

    last: BaseException | None = None
    for attempt in range(total):
        try:
            return await operation()
        except BaseException as exc:  # noqa: BLE001 — re-raised below unless 429
            if not _is_rate_limit(exc):
                raise
            last = exc
            if attempt == total - 1:
                break
            requested = _retry_after_seconds(exc)
            if requested is not None:
                delay = min(requested, ceiling)
            else:
                delay = min(base * (2**attempt), ceiling)
            # Jitter so parallel workers do not re-collide on the same window.
            delay += random.uniform(0, min(1.0, delay * 0.25))
            await asyncio.sleep(delay)

    raise RuntimeError(
        f"{what} exhausted {total} attempts against a provider rate limit. "
        "The Voyage no-payment tier allows 3 requests/minute; either slow the "
        "caller down or raise LLM_RATE_LIMIT_ATTEMPTS."
    ) from last


# ---------------------------------------------------------------------------
# calls
# ---------------------------------------------------------------------------

def _litellm():
    """Import LiteLLM lazily — it is slow to import and noisy at module scope."""
    import warnings

    # LiteLLM re-serializes provider responses through pydantic models that do
    # not match Groq's field set exactly. The warning is cosmetic and would
    # otherwise drown out demo/test output on every single call.
    warnings.filterwarnings(
        "ignore", message="Pydantic serializer warnings", category=UserWarning
    )

    import litellm

    litellm.drop_params = True
    litellm.suppress_debug_info = True
    return litellm


async def complete(
    messages: Sequence[dict[str, Any]] | str,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.2,
    timeout: float | None = None,
    num_retries: int | None = None,
    rate_limit_attempts_override: int | None = None,
    **kw: Any,
) -> str:
    """One chat completion. Returns the assistant's text content.

    ``messages`` may be a plain string for convenience; it is wrapped as a
    single user turn.

    ``max_tokens`` is clamped up to ``MIN_MAX_TOKENS`` — see the module
    docstring on the gpt-oss reasoning-budget trap.
    """
    load_env()
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]

    resolved_model = model or resolve_completion_model()
    budget = max_tokens if max_tokens is not None else default_max_tokens()
    budget = max(MIN_MAX_TOKENS, int(budget))

    litellm = _litellm()

    async def _call():
        return await litellm.acompletion(
            model=resolved_model,
            messages=list(messages),
            max_tokens=budget,
            temperature=temperature,
            timeout=timeout if timeout is not None else request_timeout(),
            num_retries=num_retries if num_retries is not None else max_retries(),
            **kw,
        )

    response = await _with_rate_limit_retry(
        _call, attempts=rate_limit_attempts_override, what=f"completion ({resolved_model})"
    )
    content = response.choices[0].message.content
    return content or ""


async def stream(
    messages: Sequence[dict[str, Any]] | str,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.2,
    timeout: float | None = None,
    num_retries: int | None = None,
    rate_limit_attempts_override: int | None = None,
    **kw: Any,
) -> AsyncIterator[str]:
    """Stream one chat completion, yielding text deltas as they arrive.

    The streaming sibling of :func:`complete`, added for M5's response graph.
    Everything that makes this module the single seam applies here too — the
    model is resolved from the environment on every call, the gpt-oss
    ``MIN_MAX_TOKENS`` floor is enforced, and 429s go through the same backoff.

    Empty deltas are skipped, so a caller can treat every yielded string as
    real output. Note gpt-oss spends its budget on reasoning *before* emitting
    content, so the first delta can lag noticeably — that is the trap in the
    module docstring, not a stalled stream.

    The rate-limit retry wraps only the **establishment** of the stream, not
    the iteration. Once tokens are flowing a 429 cannot occur, and re-running
    the operation mid-iteration would replay tokens the caller already saw.
    """
    load_env()
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]

    resolved_model = model or resolve_completion_model()
    budget = max_tokens if max_tokens is not None else default_max_tokens()
    budget = max(MIN_MAX_TOKENS, int(budget))

    litellm = _litellm()

    async def _open():
        return await litellm.acompletion(
            model=resolved_model,
            messages=list(messages),
            max_tokens=budget,
            temperature=temperature,
            timeout=timeout if timeout is not None else request_timeout(),
            num_retries=num_retries if num_retries is not None else max_retries(),
            stream=True,
            **kw,
        )

    response = await _with_rate_limit_retry(
        _open, attempts=rate_limit_attempts_override, what=f"stream ({resolved_model})"
    )

    async for chunk in response:
        choices = getattr(chunk, "choices", None)
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        piece = getattr(delta, "content", None) if delta is not None else None
        if piece:
            yield piece


async def embed(
    texts: Iterable[str] | str,
    *,
    model: str | None = None,
    timeout: float | None = None,
    num_retries: int | None = None,
    rate_limit_attempts_override: int | None = None,
    **kw: Any,
) -> list[list[float]]:
    """Embed one or more texts. Returns a list of vectors, input order preserved.

    A single string is accepted and returns a one-element list, so callers never
    have to branch on cardinality.
    """
    load_env()
    if isinstance(texts, str):
        texts = [texts]
    batch = list(texts)
    if not batch:
        return []

    resolved_model = model or resolve_embedding_model()

    litellm = _litellm()

    async def _call():
        return await litellm.aembedding(
            model=resolved_model,
            input=batch,
            timeout=timeout if timeout is not None else request_timeout(),
            num_retries=num_retries if num_retries is not None else max_retries(),
            **kw,
        )

    response = await _with_rate_limit_retry(
        _call, attempts=rate_limit_attempts_override, what=f"embedding ({resolved_model})"
    )
    items = sorted(response.data, key=lambda d: d.get("index", 0))
    return [list(item["embedding"]) for item in items]


async def health() -> dict[str, Any]:
    """Live round-trip against both providers. Used by scripts/demo_m1.sh."""
    result: dict[str, Any] = {
        "completion_model": resolve_completion_model(),
        "embedding_model": resolve_embedding_model(),
        "embedding_dim": resolve_embedding_dim(),
    }
    try:
        text = await complete("Reply with the single word: pong")
        result["completion_ok"] = bool(text.strip())
        result["completion_text"] = text.strip()[:120]
    except Exception as exc:
        result["completion_ok"] = False
        result["completion_error"] = f"{type(exc).__name__}: {exc}"

    try:
        vectors = await embed(["hello"])
        result["embedding_ok"] = bool(vectors) and len(vectors[0]) == resolve_embedding_dim()
        result["embedding_len"] = len(vectors[0]) if vectors else 0
    except Exception as exc:
        result["embedding_ok"] = False
        result["embedding_error"] = f"{type(exc).__name__}: {exc}"

    return result


if __name__ == "__main__":
    import json

    print(json.dumps(asyncio.run(health()), indent=2))
