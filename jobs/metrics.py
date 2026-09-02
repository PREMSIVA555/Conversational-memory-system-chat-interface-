"""Run-level observability for the background jobs (M8 step 12).

Two layers, because they answer different questions:

`RunStats`   the record of ONE job run — rows claimed, decayed, archived,
             summaries written, wall-clock duration, plus the set of memory ids
             this process actually processed. It is a plain dataclass that
             serialises to JSON, which is what makes the distributed test
             possible at all: three worker subprocesses each write their
             `RunStats` out, and the test asserts on the union and the pairwise
             intersections of `processed_ids`. A counter alone could never prove
             "no row was processed twice" — you need the identities.

Prometheus   the aggregate across runs, for a dashboard. Counters, not gauges,
             for everything that accumulates; a Histogram for duration.

WHY `_get_or_create`
--------------------
Same problem `store/audit.py` and `retrieve/breaker.py` document: pytest can
import one module under two names in a single process, and the default
Prometheus registry raises `ValueError: Duplicated timeseries` on the second
collector with the same name. Reimplemented here in a few lines rather than
imported from either of those, so `jobs/` does not grow a dependency on
`store/` or `retrieve/` for a logging concern.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

logger = logging.getLogger("memsys.jobs")

__all__ = [
    "RunStats",
    "configure_logging",
    "log_event",
    "ROWS_CLAIMED",
    "ROWS_DECAYED",
    "ROWS_ARCHIVED",
    "SUMMARIES_WRITTEN",
    "JOB_DURATION",
    "JOB_RUNS",
]


# ---------------------------------------------------------------------------
# structured logging
# ---------------------------------------------------------------------------

_configured = False


def configure_logging(level: int | None = None) -> None:
    """Attach a stream handler to the `memsys.jobs` logger, once.

    The worker subprocesses spawned by `tests/distributed/` are asserted on
    through their stdout — the Definition of Done asks to read the worker/pid
    lines — so a job that logs nothing because the root logger sits at WARNING
    would make the milestone unverifiable.
    """
    global _configured
    if _configured:
        return
    resolved = level if level is not None else getattr(
        logging, os.environ.get("JOBS_LOG_LEVEL", "INFO").upper(), logging.INFO
    )
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(resolved)
    logger.propagate = False
    _configured = True


def log_event(event: str, **fields: Any) -> None:
    """One structured line. Values are JSON-encoded so logs stay greppable."""
    payload = json.dumps(fields, default=str, sort_keys=True)
    logger.info("%s %s", event, payload)


# ---------------------------------------------------------------------------
# prometheus collectors
# ---------------------------------------------------------------------------

def _get_or_create(factory, name: str):
    try:
        return factory()
    except ValueError:
        from prometheus_client import REGISTRY

        existing = REGISTRY._names_to_collectors.get(name)
        if existing is None:  # pragma: no cover
            raise
        return existing


def _counter(name: str, doc: str, labels: tuple[str, ...] = ()):
    def build():
        from prometheus_client import Counter

        return Counter(name, doc, list(labels))

    return _get_or_create(build, name)


def _histogram(name: str, doc: str, labels: tuple[str, ...] = ()):
    def build():
        from prometheus_client import Histogram

        return Histogram(name, doc, list(labels))

    return _get_or_create(build, name)


ROWS_CLAIMED = _counter(
    "memsys_decay_rows_claimed_total", "Rows claimed by decay workers", ("job",)
)
ROWS_DECAYED = _counter(
    "memsys_decay_rows_decayed_total", "Rows whose weight was updated by decay", ("job",)
)
ROWS_ARCHIVED = _counter(
    "memsys_decay_rows_archived_total", "Rows archived below ARCHIVE_THRESHOLD", ("job",)
)
SUMMARIES_WRITTEN = _counter(
    "memsys_reflection_summaries_total", "Reflection summary memories written", ("job",)
)
JOB_RUNS = _counter(
    "memsys_job_runs_total", "Background job runs, by job and outcome", ("job", "outcome")
)
JOB_DURATION = _histogram(
    "memsys_job_duration_seconds", "Wall-clock duration of one job run", ("job",)
)


# ---------------------------------------------------------------------------
# the per-run record
# ---------------------------------------------------------------------------

@dataclass
class RunStats:
    """Everything one job run did. Serialises to JSON for the distributed test.

    `processed_ids` is a *list*, not a count, and that is the point — see the
    module docstring. It is kept ordered by processing order so a reader can
    see the claim sequence, and `add_processed()` refuses a repeat within one
    run, which turns an in-process double-process into a loud error rather than
    a silently-correct-looking count.
    """

    job: str
    run_id: str
    worker: str = "worker-0"
    pid: int = field(default_factory=os.getpid)

    batches: int = 0
    rows_claimed: int = 0
    rows_decayed: int = 0
    rows_archived: int = 0
    summaries_written: int = 0
    sources_consolidated: int = 0

    processed_ids: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    error: str | None = None

    _started: float = field(default=0.0, repr=False)
    _seen: set[str] = field(default_factory=set, repr=False)

    # -- timing ------------------------------------------------------------

    def start(self) -> "RunStats":
        self._started = time.perf_counter()
        return self

    def finish(self, outcome: str = "ok") -> "RunStats":
        if self._started:
            self.duration_seconds = round(time.perf_counter() - self._started, 4)
        JOB_DURATION.labels(job=self.job).observe(self.duration_seconds)
        JOB_RUNS.labels(job=self.job, outcome=outcome).inc()
        return self

    # -- accounting --------------------------------------------------------

    def add_processed(self, memory_ids: Iterable[str]) -> None:
        """Record ids this worker processed. Raises on a same-run repeat."""
        for memory_id in memory_ids:
            key = str(memory_id)
            if key in self._seen:
                raise RuntimeError(
                    f"decay run {self.run_id} processed memory {key} twice in worker "
                    f"{self.worker} (pid {self.pid}). The claim query is supposed to "
                    "make this impossible — see jobs/claims.py."
                )
            self._seen.add(key)
            self.processed_ids.append(key)

    def count_claimed(self, n: int) -> None:
        self.rows_claimed += n
        ROWS_CLAIMED.labels(job=self.job).inc(n)

    def count_decayed(self, n: int) -> None:
        self.rows_decayed += n
        ROWS_DECAYED.labels(job=self.job).inc(n)

    def count_archived(self, n: int) -> None:
        self.rows_archived += n
        ROWS_ARCHIVED.labels(job=self.job).inc(n)

    def count_summary(self, n: int = 1) -> None:
        self.summaries_written += n
        SUMMARIES_WRITTEN.labels(job=self.job).inc(n)

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    def log(self) -> None:
        """One line per run, with the worker/pid identity the DoD asks to read."""
        log_event(
            f"{self.job}.run.complete",
            run_id=self.run_id,
            worker=self.worker,
            pid=self.pid,
            batches=self.batches,
            rows_claimed=self.rows_claimed,
            rows_decayed=self.rows_decayed,
            rows_archived=self.rows_archived,
            summaries_written=self.summaries_written,
            sources_consolidated=self.sources_consolidated,
            duration_seconds=self.duration_seconds,
            error=self.error,
        )
