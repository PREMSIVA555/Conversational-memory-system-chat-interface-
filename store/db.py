"""Async connection pooling and RLS session scoping.

Two distinct DSNs live here, and the distinction is the whole point:

``admin_dsn()``  -> ``DATABASE_URL``. The owner/superuser role. Used ONLY by
                    ``python -m store.migrate`` for DDL.

``app_dsn()``    -> ``APP_DATABASE_URL`` if set, otherwise ``DATABASE_URL`` with
                    the credentials swapped for ``APP_DB_USER`` /
                    ``APP_DB_PASSWORD``. This is a NON-superuser, NON-owner role
                    created by migration 0004. Everything that reads or writes
                    application data connects through it, because PostgreSQL
                    superusers bypass row-level security unconditionally — an
                    app that connects as the owner has RLS policies that are
                    decorative rather than enforcing.

``session()`` is the only sanctioned way to touch application tables: it opens a
transaction and sets the ``app.subject_id`` / ``app.actor_id`` GUCs that every
RLS policy predicate reads. The GUCs are set with ``is_local=true`` so they are
scoped to the transaction and cannot leak to the next borrower of the pooled
connection.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote, urlsplit, urlunsplit

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

_ENV_PATH = Path(__file__).resolve().parent.parent / "infra" / ".env"
_env_loaded = False


def ensure_selector_event_loop_policy() -> None:
    """psycopg's async driver cannot run on Windows' default ProactorEventLoop.

    It needs a selector-based loop. This has to be called BEFORE the event loop
    is created — at import time in conftest.py, and before ``uvicorn.run()`` in
    ``api/main.py`` — because swapping the policy afterwards has no effect on an
    already-running loop. It is a no-op everywhere but Windows.
    """
    if sys.platform != "win32":
        return
    policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy is None:  # pragma: no cover - non-Windows
        return
    if not isinstance(asyncio.get_event_loop_policy(), policy):
        asyncio.set_event_loop_policy(policy())


ensure_selector_event_loop_policy()


def load_env(override: bool = False) -> None:
    """Load ``infra/.env`` once. Real environment variables win by default."""
    global _env_loaded
    if _env_loaded and not override:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is a hard dependency
        _env_loaded = True
        return
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH, override=override)
    _env_loaded = True


# ---------------------------------------------------------------------------
# DSNs
# ---------------------------------------------------------------------------

def admin_dsn() -> str:
    """Owner/superuser DSN. Migrations only."""
    load_env()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set (see infra/.env.example)")
    return dsn


def app_dsn() -> str:
    """Non-superuser application DSN. RLS applies to this role."""
    load_env()
    explicit = os.environ.get("APP_DATABASE_URL")
    if explicit:
        return explicit

    user = app_db_user()
    password = app_db_password()

    parts = urlsplit(admin_dsn())
    host = parts.hostname or "localhost"
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def app_db_user() -> str:
    load_env()
    return os.environ.get("APP_DB_USER", "memory_app")


def app_db_password() -> str:
    """Password for the non-superuser app role.

    Deliberately has **no default**. An earlier version defaulted to the same
    literal that ships in ``infra/.env``, which meant the live credential for the
    role that RLS depends on sat in tracked source. A missing value must fail
    loudly here rather than silently authenticate.
    """
    load_env()
    password = os.environ.get("APP_DB_PASSWORD")
    if not password:
        raise RuntimeError(
            "APP_DB_PASSWORD is not set. Copy infra/.env.example to infra/.env and "
            "set it (see README). No default is provided on purpose."
        )
    return password


def redis_url() -> str:
    load_env()
    return os.environ.get("REDIS_URL", "redis://localhost:56379/0")


def embedding_dim() -> int:
    load_env()
    return int(os.environ.get("EMBEDDING_DIM", "1024"))


# ---------------------------------------------------------------------------
# pool factory
# ---------------------------------------------------------------------------

_pools: dict[str, AsyncConnectionPool] = {}

# One lock per DSN, guarding the create-and-store critical section below.
# asyncio.Lock binds to the running loop on first use (not at construction) on
# Python 3.10+, so building these lazily at module scope is safe here.
_pool_locks: dict[str, asyncio.Lock] = {}


async def get_pool(dsn: str | None = None, *, min_size: int = 1, max_size: int = 10) -> AsyncConnectionPool:
    """Return (creating on first call) the async pool for ``dsn``.

    Defaults to the *application* DSN, not the admin one — callers have to opt
    in explicitly to the privileged connection.

    Creation is serialized per-DSN. The obvious version of this function checks
    ``_pools``, awaits ``pool.open()``, then stores the result — but that check
    and that store straddle an await, so two concurrent first-touch callers both
    see an empty cache and both *open* a pool. Only the last one is recorded;
    the other is orphaned holding up to ``max_size`` connections, invisible to
    ``close_pools()``, for the life of the process. It stayed latent until the
    hybrid retriever became the first code here to hit the DB concurrently.
    """
    dsn = dsn or app_dsn()

    pool = _pools.get(dsn)
    if pool is not None and not pool.closed:
        return pool

    lock = _pool_locks.setdefault(dsn, asyncio.Lock())
    async with lock:
        # Re-check inside the lock: a racing caller may have finished while we waited.
        pool = _pools.get(dsn)
        if pool is not None and not pool.closed:
            return pool

        pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={"row_factory": dict_row},
        )
        try:
            await pool.open(wait=True, timeout=15.0)
        except BaseException:
            # Never leave a half-open pool behind if open() fails or is cancelled.
            await pool.close()
            raise
        _pools[dsn] = pool
        return pool


async def close_pools() -> None:
    for dsn, pool in list(_pools.items()):
        await pool.close()
        _pools.pop(dsn, None)


# ---------------------------------------------------------------------------
# RLS-scoped session
# ---------------------------------------------------------------------------

@asynccontextmanager
async def session(subject_id: str, actor_id: str, *, dsn: str | None = None) -> AsyncIterator:
    """Open a transaction with the RLS GUCs set for ``subject_id``/``actor_id``.

    Yields a live ``psycopg`` connection. The GUCs are transaction-local
    (``set_config(..., is_local => true)``), so they are reset automatically when
    the transaction ends and never leak into the next user of this pooled
    connection.
    """
    pool = await get_pool(dsn)
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.subject_id', %s, true),"
                "       set_config('app.actor_id',   %s, true)",
                (str(subject_id), str(actor_id)),
            )
            yield conn


@asynccontextmanager
async def admin_session() -> AsyncIterator:
    """Owner connection, no RLS GUCs. DDL and catalog inspection only."""
    pool = await get_pool(admin_dsn())
    async with pool.connection() as conn:
        yield conn


# ---------------------------------------------------------------------------
# health probes
# ---------------------------------------------------------------------------

def _sync_ping_postgres(timeout: float) -> bool:
    import psycopg

    try:
        with psycopg.connect(app_dsn(), connect_timeout=max(1, int(timeout))) as conn:
            row = conn.execute("SELECT 1").fetchone()
            return bool(row and row[0] == 1)
    except Exception:
        return False


async def ping_postgres(timeout: float = 3.0) -> bool:
    """True if the *application* role can reach PostgreSQL.

    Deliberately runs the synchronous driver on a worker thread rather than
    psycopg's async driver: the probe then works under any event loop policy,
    including a host process that forgot to call
    ``ensure_selector_event_loop_policy()`` on Windows. A health check that
    reports 'down' because of the caller's loop policy would be worse than
    useless.

    It probes as the app role, not the owner — so a broken RLS grant shows up
    here rather than at the first real query.
    """
    return await asyncio.to_thread(_sync_ping_postgres, timeout)


async def ping_redis(timeout: float = 3.0) -> bool:
    """True if Redis answers PING."""
    try:
        import redis.asyncio as aioredis
    except ImportError:
        return False

    client = aioredis.from_url(
        redis_url(), socket_connect_timeout=timeout, socket_timeout=timeout
    )
    try:
        return bool(await client.ping())
    except Exception:
        return False
    finally:
        try:
            await client.aclose()
        except Exception:
            pass
