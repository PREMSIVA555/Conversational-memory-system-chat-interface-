"""FastAPI application: /health and /metrics.

    uvicorn api.main:app --host 0.0.0.0 --port 8000

``GET /health`` always answers 200 with per-dependency booleans rather than
failing the request when a dependency is down — a health endpoint that itself
becomes unreachable when Postgres blinks tells you nothing. The ``status`` field
is ``"ok"`` only when every dependency is reachable.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from api.chat import router as chat_router
from capture.metrics import configure_logging
from llm.config import resolve_completion_model, resolve_embedding_model
from store.db import (
    close_pools,
    ensure_selector_event_loop_policy,
    load_env,
    ping_postgres,
    ping_redis,
)

load_env()

# Plan step 13: without this the `memsys.capture` logger inherits root's default
# WARNING level and every structured capture log line is discarded before a
# handler sees it. Honours LOG_LEVEL from infra/.env.
configure_logging()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await close_pools()


app = FastAPI(
    title="memory-system",
    version="0.1.0",
    description="Persistent conversational-memory layer (M1: infra + schema + LLM seam)",
    lifespan=lifespan,
)

app.include_router(chat_router)  # M2: POST /chat

# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

HEALTH_CHECKS = Counter(
    "memsys_health_checks_total", "Health endpoint calls", ["status"]
)
DEPENDENCY_UP = Gauge(
    "memsys_dependency_up", "1 when the dependency answered its probe", ["dependency"]
)
HEALTH_LATENCY = Histogram(
    "memsys_health_latency_seconds", "Wall time of a full /health dependency sweep"
)


@app.get("/health")
async def health() -> dict:
    """Liveness + per-dependency readiness. Always 200."""
    started = time.perf_counter()
    postgres_ok, redis_ok = await asyncio.gather(
        ping_postgres(), ping_redis(), return_exceptions=False
    )
    elapsed = time.perf_counter() - started

    HEALTH_LATENCY.observe(elapsed)
    DEPENDENCY_UP.labels(dependency="postgres").set(1 if postgres_ok else 0)
    DEPENDENCY_UP.labels(dependency="redis").set(1 if redis_ok else 0)

    status = "ok" if (postgres_ok and redis_ok) else "degraded"
    HEALTH_CHECKS.labels(status=status).inc()

    return {
        "status": status,
        "postgres": bool(postgres_ok),
        "redis": bool(redis_ok),
        "models": {
            "completion": resolve_completion_model(),
            "embedding": resolve_embedding_model(),
        },
        "latency_ms": round(elapsed * 1000, 2),
    }


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus scrape target. infra/prometheus.yml points here."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    import uvicorn

    # Must happen before uvicorn creates the loop — see store.db for why.
    ensure_selector_event_loop_policy()

    # The app OBJECT, not the "api.main:app" import string. Under
    # `python -m api.main` this module is already loaded as __main__; handing
    # uvicorn the string would make it import the module a second time under
    # its real name and re-register every Prometheus collector, which the
    # default registry rejects.
    uvicorn.run(
        app,
        host=os.environ.get("API_HOST", "0.0.0.0"),
        port=int(os.environ.get("API_PORT", "8000")),
        reload=False,
    )
