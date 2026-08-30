"""Repo-root pytest bootstrap.

Puts the project root on ``sys.path`` (so ``import store`` / ``import llm``
works without an editable install) and loads ``infra/.env`` before any test
module imports, so provider keys and DSNs are present.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from store.db import ensure_selector_event_loop_policy, load_env  # noqa: E402

# Before pytest-asyncio creates any event loop: psycopg's async driver cannot
# run on Windows' ProactorEventLoop.
ensure_selector_event_loop_policy()
load_env()


@pytest.fixture(autouse=True)
def _reset_litellm_client_cache():
    """Drop LiteLLM's cached async HTTP client before every test.

    WHY THIS EXISTS — it fixes a real cross-test failure, not a cosmetic one.

    LiteLLM memoises the ``httpx.AsyncClient`` it builds per provider in the
    process-global ``litellm.in_memory_llm_clients_cache``. That client binds to
    whatever event loop was running when it was created. pytest-asyncio builds a
    **fresh event loop per test function** and closes the previous one, so the
    second test to call a given provider reuses a client whose loop is gone and
    dies with ``RuntimeError: Event loop is closed``.

    It only became visible once M2 landed: ``test_capture_graph.py`` sorts before
    ``test_m1_infra.py``, so capture's embedding calls warm the cache first and
    M1's ``test_litellm_embedding_returns_nonempty_vector`` inherited the dead
    client. M1 passed 10/10 in isolation — the signature of shared state rather
    than a broken assertion.

    Note this failure mode is easy to misread as a rate limit: both present as
    "fails in a full run, passes alone". The distinguishing evidence is the
    ``Event loop is closed`` traceback rather than a 429.

    Lives at repo root (not in ``tests/integration/``) so unit tests making live
    provider calls are covered too. Flushing per test costs one TCP handshake.
    """
    try:
        import litellm

        cache = getattr(litellm, "in_memory_llm_clients_cache", None)
        if cache is not None and hasattr(cache, "flush_cache"):
            cache.flush_cache()
    except Exception:  # pragma: no cover — never fail a test on cleanup
        pass
    yield
