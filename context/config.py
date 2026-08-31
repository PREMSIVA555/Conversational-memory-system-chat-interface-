"""Tunables and the block template for the context composer (plan step 11).

Same convention as `retrieve/config.py`: env-backed values are read at call time
so a test can monkeypatch and see the change without a module reload, and the
defaults live here and nowhere else.

The template constants are plain module constants rather than env lookups. They
are prompt text, not configuration — changing them changes what the model reads
and belongs in a diff someone reviewed, not in a `.env` file.
"""

from __future__ import annotations

import os

from store.db import load_env

DEFAULTS = {
    # How many tokens the whole rendered profile block may occupy — header,
    # delimiters and all (plan step 9). 512 out of the model's context is a
    # deliberate ceiling rather than a technical limit: the block is prepended to
    # every single turn, so its cost is paid on every request forever, and past
    # roughly this size the memories stop being background and start competing
    # with the user's actual message for the model's attention.
    "TOKEN_BUDGET": "512",
    # Tokenizer encoding used when the configured LLM_MODEL is not one tiktoken
    # knows. See `context/tokens.py`.
    "FALLBACK_ENCODING": "o200k_base",
}


def _env(name: str) -> str:
    load_env()
    value = os.environ.get(f"CONTEXT_{name}")
    if value is None or value == "":
        return DEFAULTS[name]
    return value


def token_budget() -> int:
    """The composer's hard ceiling, in tokens, for the entire rendered block."""
    return int(_env("TOKEN_BUDGET"))


def fallback_encoding() -> str:
    return _env("FALLBACK_ENCODING")


# Module-level alias, matching `retrieve/config.py`'s style.
TOKEN_BUDGET = token_budget


# ---------------------------------------------------------------------------
# the block template (plan step 11)
# ---------------------------------------------------------------------------
#
# Rendered shape:
#
#     ## What you know about the user
#     - The user plays the cello.
#     - The user's daughter is named Priya.
#
# The header is not free — it costs tokens, and plan step 9 requires those
# tokens to be counted INSIDE the budget rather than added on top of it. The
# composer therefore renders the complete block and counts that, rather than
# summing per-line counts and hoping the overhead was small.

BLOCK_HEADER = "## What you know about the user"

# Per-memory line. `{content}` is the only field: a memory is a sentence, and
# decorating it with ids or scores would spend budget on tokens that help the
# developer rather than the model.
LINE_TEMPLATE = "- {content}"

# Joined between the header and each line.
LINE_SEPARATOR = "\n"
