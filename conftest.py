"""Repo-root pytest bootstrap.

Puts the project root on ``sys.path`` (so ``import store`` / ``import llm``
works without an editable install) and loads ``infra/.env`` before any test
module imports, so provider keys and DSNs are present.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from store.db import ensure_selector_event_loop_policy, load_env  # noqa: E402

# Before pytest-asyncio creates any event loop: psycopg's async driver cannot
# run on Windows' ProactorEventLoop.
ensure_selector_event_loop_policy()
load_env()
