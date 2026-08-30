"""Structured logging + Prometheus instrumentation for the capture graph (plan step 13).

Everything the pipeline does is observable through two channels:

* the ``memsys.capture`` logger, which emits one structured line per node with
  the node name, the subject, the in/out cardinality and the wall time;
* the Prometheus counters below, scraped through the existing ``/metrics``
  endpoint on the FastAPI app.

Collectors are registered against the default registry at import time. The
module is import-guarded (`_REGISTERED`) because pytest can import a module
under two different names in one process, and the default registry raises on a
duplicate collector name.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from typing import Any, Iterator

from prometheus_client import Counter, Histogram

from store.db import load_env

#: Root of the application's logger tree. `memsys.capture` is a child, so
#: setting the level here governs every capture log line.
LOGGER_NAMESPACE = "memsys"

log = logging.getLogger("memsys.capture")


def configure_logging(level: str | None = None) -> logging.Logger:
    """Make `log_event()` actually emit. Idempotent; safe to call repeatedly.

    WHY THIS IS NEEDED -- without it every structured log line in this package
    is silently discarded. A bare `logging.getLogger("memsys.capture")` has
    level NOTSET and inherits the root logger's default of WARNING, so
    `isEnabledFor(INFO)` is False and every `log_event()` call is dropped before
    a handler ever sees it. The Prometheus half of the instrumentation worked
    while the logging half was inert.

    HANDLER POLICY -- a handler is attached **only** when nothing else is set up
    to emit records, and `propagate` is left True. That keeps one behaviour in
    three very different hosts:

      * under uvicorn, the root logger already has handlers, so we add none and
        our records propagate into the server's existing log stream (no
        duplicate lines);
      * under pytest, the logging plugin's root handlers capture propagated
        records, so `caplog` and `--log-cli-level` both work;
      * under a bare `python -m ...` script, root has no handlers, so we install
        one and output still appears.

    The level comes from `LOG_LEVEL` in `infra/.env` (already `INFO`), with an
    explicit argument overriding it.
    """
    load_env()
    resolved = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()

    logger = logging.getLogger(LOGGER_NAMESPACE)
    logger.setLevel(resolved)

    root_has_handlers = bool(logging.getLogger().handlers)
    ours_already_attached = any(getattr(h, "_memsys", False) for h in logger.handlers)

    if not root_has_handlers and not ours_already_attached:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
        )
        handler._memsys = True  # type: ignore[attr-defined]
        logger.addHandler(handler)

    logger.propagate = True
    return logger

# ---------------------------------------------------------------------------
# counters
# ---------------------------------------------------------------------------

CANDIDATES = Counter(
    "memsys_capture_candidates_total",
    "Candidate facts entering and leaving each capture node",
    ["node", "direction"],
)
PII_ENTITIES = Counter(
    "memsys_capture_pii_entities_total",
    "PII entities detected and redacted, by Presidio entity type",
    ["entity_type"],
)
DEDUP_OUTCOMES = Counter(
    "memsys_capture_dedup_total",
    "Dedup verdicts by outcome",
    ["outcome"],  # new | duplicate
)
WRITES = Counter(
    "memsys_capture_writes_total",
    "Terminal write actions",
    ["action"],  # insert | reinforce
)
JOBS = Counter(
    "memsys_capture_jobs_total",
    "Capture jobs processed by the background worker",
    ["status"],  # completed | failed | timeout | dropped
)
NODE_LATENCY = Histogram(
    "memsys_capture_node_latency_seconds",
    "Wall time of one capture node",
    ["node"],
)


def _emit(level: int, event: str, **fields: Any) -> None:
    """One structured log line. JSON payload so it is greppable and parseable."""
    try:
        payload = json.dumps({"event": event, **fields}, default=str, sort_keys=True)
    except Exception:  # pragma: no cover - defensive; logging must never raise
        payload = f'{{"event": "{event}"}}'
    log.log(level, payload)


def log_event(event: str, **fields: Any) -> None:
    _emit(logging.INFO, event, **fields)


def log_warning(event: str, **fields: Any) -> None:
    _emit(logging.WARNING, event, **fields)


@contextmanager
def node_span(node: str, subject_id: str, n_in: int) -> Iterator[dict[str, Any]]:
    """Time one node, count candidates in/out, and log the result.

    Yields a mutable dict; the node sets ``result["out"]`` (and any extra
    fields worth logging) before the block exits.
    """
    started = time.perf_counter()
    CANDIDATES.labels(node=node, direction="in").inc(n_in)
    result: dict[str, Any] = {"out": 0}
    try:
        yield result
    except Exception as exc:
        NODE_LATENCY.labels(node=node).observe(time.perf_counter() - started)
        _emit(
            logging.ERROR,
            "capture.node.error",
            node=node,
            subject_id=subject_id,
            candidates_in=n_in,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    n_out = int(result.get("out") or 0)
    elapsed = time.perf_counter() - started
    NODE_LATENCY.labels(node=node).observe(elapsed)
    CANDIDATES.labels(node=node, direction="out").inc(n_out)
    extra = {k: v for k, v in result.items() if k != "out"}
    _emit(
        logging.INFO,
        "capture.node",
        node=node,
        subject_id=subject_id,
        candidates_in=n_in,
        candidates_out=n_out,
        dropped=max(0, n_in - n_out),
        elapsed_ms=round(elapsed * 1000, 2),
        **extra,
    )
