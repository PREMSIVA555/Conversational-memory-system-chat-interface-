"""Tunable knobs for the capture pipeline (plan step 14).

Every value is read from the environment **on every call**, never frozen at
import time. Tests monkeypatch these env vars to drive edge cases (a confidence
floor of 1.0 to prove nothing survives, a dedup threshold of 0.0 to prove
everything collapses), and a value captured at import would silently ignore them.

Defaults are chosen against the configured 1024-dim embedder:

``DEDUP_COSINE_THRESHOLD = 0.82``
    Cosine similarity between two *rewordings of the same fact* lands around
    0.85-0.95; between two genuinely unrelated personal facts it sits around
    0.35-0.65. 0.82 is inside that gap, closer to the duplicate side so the
    threshold is not over-aggressive -- `test_distinct_facts_create_separate_rows`
    is the guard against pushing it lower.

``CONFIDENCE_FLOOR = 0.45``
    Below this the extractor is guessing rather than reporting; such candidates
    are dropped before they can ever reach the write node.
"""

from __future__ import annotations

import os

from store.db import load_env

DEFAULTS = {
    "CAPTURE_CONFIDENCE_FLOOR": "0.45",
    "CAPTURE_DEDUP_COSINE_THRESHOLD": "0.82",
    "CAPTURE_MAX_CANDIDATES_PER_TURN": "8",
    # Generous because `capture/embed.py` may sit out several provider
    # rate-limit windows (up to ~275s of backoff) inside a single run. Capture
    # is off the request path, so this budget delays nobody's reply.
    "CAPTURE_TIMEOUT_SECONDS": "600",
    "CAPTURE_WORKER_CONCURRENCY": "2",
    "CAPTURE_QUEUE_MAXSIZE": "256",
    "CAPTURE_WEIGHT_INCREMENT": "0.25",
    "CAPTURE_WEIGHT_MAX": "5.0",
    "CAPTURE_PII_SCORE_THRESHOLD": "0.30",
    "CAPTURE_LOCK_TIMEOUT_MS": "30000",
}


def _env(name: str) -> str:
    load_env()
    value = os.environ.get(name)
    if value is None or value == "":
        return DEFAULTS[name]
    return value


def confidence_floor() -> float:
    """Candidates scoring below this confidence are dropped in `evaluate`."""
    return float(_env("CAPTURE_CONFIDENCE_FLOOR"))


def dedup_cosine_threshold() -> float:
    """At or above this cosine similarity a candidate reinforces instead of inserting."""
    return float(_env("CAPTURE_DEDUP_COSINE_THRESHOLD"))


def max_candidates_per_turn() -> int:
    """Hard cap on facts taken from a single turn, so one turn cannot flood the store."""
    return int(_env("CAPTURE_MAX_CANDIDATES_PER_TURN"))


def capture_timeout_seconds() -> float:
    """Wall-clock budget for one whole capture graph run inside the worker."""
    return float(_env("CAPTURE_TIMEOUT_SECONDS"))


def worker_concurrency() -> int:
    """How many capture jobs the worker runs at once.

    Deliberately > 1: with a single consumer the queue would serialise every
    job and the concurrency control in `store/memories.py` would never actually
    be exercised in production, only in tests.
    """
    return max(1, int(_env("CAPTURE_WORKER_CONCURRENCY")))


def queue_maxsize() -> int:
    return int(_env("CAPTURE_QUEUE_MAXSIZE"))


def weight_increment() -> float:
    """How much `weight` climbs per reinforcement."""
    return float(_env("CAPTURE_WEIGHT_INCREMENT"))


def weight_max() -> float:
    """Ceiling on `weight`, so a spammed fact cannot dominate ranking in M4."""
    return float(_env("CAPTURE_WEIGHT_MAX"))


def lock_timeout_ms() -> int:
    """How long a capture write waits for another writer's advisory lock.

    The lock is only ever held across a similarity query plus one INSERT or
    UPDATE -- no provider calls happen inside it -- so a real wait is
    milliseconds. Anything approaching this bound means something is genuinely
    wedged, and failing the job loudly beats blocking a worker forever.
    """
    return int(_env("CAPTURE_LOCK_TIMEOUT_MS"))


def pii_score_threshold() -> float:
    """Minimum Presidio confidence for an entity to be treated as PII.

    Low on purpose. A false positive costs one over-redacted word; a false
    negative writes a real SSN into a database column.
    """
    return float(_env("CAPTURE_PII_SCORE_THRESHOLD"))
