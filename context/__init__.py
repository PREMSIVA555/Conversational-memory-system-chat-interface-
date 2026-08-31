"""Context composition: turn ranked memories into a token-bounded prompt block.

`compose()` is the entry point M5's response graph calls. It returns a
`ComposedContext` with both the rendered block and the ids of the memories in
it — see `context/composer.py`.

    from context import compose
    result = compose(candidates)
    result.block        # text to prepend to the prompt
    result.memory_ids   # what the model was shown (M7 audit)

Submodules are imported lazily through `__getattr__` so that `import context`
does not drag in `tiktoken` (and, through the composer, `retrieve`) for a caller
that only wants `context.config`.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

__all__ = ["ComposedContext", "compose", "compose_profile_block", "count_tokens", "config"]

if TYPE_CHECKING:  # pragma: no cover
    from context.composer import ComposedContext, compose, compose_profile_block
    from context.tokens import count_tokens

# attribute name -> submodule that defines it.
_LAZY = {
    "ComposedContext": "context.composer",
    "compose": "context.composer",
    "compose_profile_block": "context.composer",
    "count_tokens": "context.tokens",
}

# Submodules reachable as `from context import <name>`. Imported through
# `importlib` rather than a `from context import ...` statement: that statement
# looks up the attribute on this module first, which lands back in `__getattr__`
# and recurses until the stack runs out.
_SUBMODULES = {"config", "composer", "tokens"}


def __getattr__(name: str):
    if name in _LAZY:
        return getattr(importlib.import_module(_LAZY[name]), name)
    if name in _SUBMODULES:
        return importlib.import_module(f"context.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
