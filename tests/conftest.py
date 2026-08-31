"""Fixtures shared by every test directory under ``tests/``.

Lives here rather than in ``tests/integration/`` so that the suites added by
later milestones — ``tests/reliability/`` (M5), ``tests/acceptance/`` (M7),
``tests/distributed/`` (M8) — inherit it without each having to rediscover the
problem it solves.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
async def _isolate_loop_bound_resources():
    """Release every event-loop-bound resource at the end of each test.

    pytest-asyncio creates a fresh event loop per test function and closes the
    previous one, but two things outlive it:

      * ``store.db`` memoises its ``AsyncConnectionPool`` in a module-global
        dict keyed by DSN. A pool built on test 1's loop is not ``closed``, so
        test 2 reuses it — and its connections raise ``RuntimeError: Event loop
        is closed``. Those connections are never returned to the pool, so it
        drains one test at a time until some later test dies with
        ``PoolTimeout: couldn't get a connection after 30.00 sec``.

        Note the failure lands on whichever test happens to be running when the
        last connection goes bad — **not** on the test that caused it. It was
        found when adding two unrelated parametrised cases upstream moved the
        failure onto the worker-pool test. Any suite touching Postgres across
        more than roughly ten tests will hit this eventually.

      * the capture worker holds consumer tasks and an ``asyncio.Queue`` bound
        to that same loop.

    Declared **autouse and first on purpose**: pytest sets autouse fixtures up
    before the rest and therefore finalises them last, so the worker's tasks are
    cancelled and the pools closed only after per-test fixtures have finished
    using them.

    Costs one pool rebuild per test — far cheaper than the cross-test flakiness
    it removes. Unit tests that never open a pool pay effectively nothing, since
    both teardown calls are no-ops on an empty registry.

    Originally written by the M2 agent for ``tests/integration/`` and promoted
    here so later suites inherit it.
    """
    yield

    # Imported lazily: a unit test that never touches these should not pay the
    # import cost, and an import error here must not mask a real failure.
    try:
        from capture.worker import reset_worker

        await reset_worker()
    except Exception:  # pragma: no cover — teardown must never mask a failure
        pass

    try:
        from store.db import close_pools

        await close_pools()
    except Exception:  # pragma: no cover
        pass
