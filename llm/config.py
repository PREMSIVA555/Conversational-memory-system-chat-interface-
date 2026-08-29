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
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Iterable, Sequence

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
    response = await litellm.acompletion(
        model=resolved_model,
        messages=list(messages),
        max_tokens=budget,
        temperature=temperature,
        timeout=timeout if timeout is not None else request_timeout(),
        num_retries=num_retries if num_retries is not None else max_retries(),
        **kw,
    )
    content = response.choices[0].message.content
    return content or ""


async def embed(
    texts: Iterable[str] | str,
    *,
    model: str | None = None,
    timeout: float | None = None,
    num_retries: int | None = None,
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
    response = await litellm.aembedding(
        model=resolved_model,
        input=batch,
        timeout=timeout if timeout is not None else request_timeout(),
        num_retries=num_retries if num_retries is not None else max_retries(),
        **kw,
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
