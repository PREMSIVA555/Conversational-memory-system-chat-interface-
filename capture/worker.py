"""Async capture worker (plan step 11) -- the thing that keeps capture off the request path.

The request handler's only interaction with capture is `enqueue()`, which is a
plain `Queue.put_nowait` and is **not** a coroutine. There is nothing to await,
so there is no way for the handler to end up waiting on extraction, embedding
or a database round-trip, however slow those become. The graph runs later, on
separate long-lived consumer tasks.

`test_capture_does_not_block_response` is the guard: it slows the capture graph
to several seconds and asserts the chat response still completes promptly, then
asserts the capture nonetheless happened afterwards.

CONCURRENCY -- `CAPTURE_WORKER_CONCURRENCY` consumers run in parallel (default 2)
rather than one. A single consumer would serialise every job, which would mean
the advisory-lock concurrency control in `store/memories.py` was only ever
exercised by tests and never in production.

EVENT-LOOP REBINDING -- `ensure_started()` compares the stored loop against the
running one and rebuilds the queue and tasks when they differ. pytest-asyncio
creates a fresh event loop per test function, and a module-level worker holding
a queue and tasks bound to a closed loop would hang the next test.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Awaitable, Callable, Optional

from prometheus_client import Gauge

from capture import config as capture_config
from capture.metrics import JOBS, log_event, log_warning
from graphs.capture_graph import run_capture
from graphs.capture_state import CaptureState, Turn

QUEUE_DEPTH = Gauge(
    "memsys_capture_queue_depth",
    "Capture jobs waiting in the in-process worker queue",
)


class CaptureWorkerNotOnEventLoop(RuntimeError):
    """The worker was touched from a thread with no running event loop.

    Deliberately NOT made "robust" by silently tolerating the situation. The
    worker's whole contract is that an enqueued turn is eventually captured; a
    queue created off-loop would have no consumers draining it, so enqueuing
    into it would accept the job and then lose it. A loud failure beats silent
    data loss -- the production incident this replaces was exactly that shape,
    except the reply had already been generated when it blew up.

    Running a private background loop in its own thread would make it work, but
    that is an architectural change with real hazards (psycopg connections are
    bound to the loop that created them), and it is not M2's to make.
    """


class CaptureJob(dict):
    """A queued turn. `{'job_id', 'subject_id', 'actor_id', 'turn'}`."""


class CaptureWorker:
    """An in-process asyncio consumer pool for capture jobs."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue] = None
        self._tasks: list[asyncio.Task] = []
        #: Rolling record of completed jobs. Bounded, so a long-lived process
        #: cannot grow it without limit. Tests read it; production ignores it.
        self.completed: list[dict[str, Any]] = []
        self._max_completed = 128

    # -- lifecycle ---------------------------------------------------------

    def ensure_started(self) -> None:
        """Bind to the running loop, rebuilding the pool if the loop changed.

        Raises `CaptureWorkerNotOnEventLoop` when called off-loop -- see that
        class for the trap this names.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise CaptureWorkerNotOnEventLoop(
                "CaptureWorker.ensure_started() was called with no running event loop. "
                "The worker owns an asyncio.Queue and consumer tasks, which can only be "
                "created on a loop.\n\n"
                "The usual cause is a SYNC callable handed to something that dispatches "
                "it to a thread -- most often starlette.background.BackgroundTask, which "
                "inspects the callable and sends a plain `def` through run_in_threadpool. "
                "In that worker thread there is no loop and this raises.\n\n"
                "Fix: wrap the callable in an `async def`. Starlette awaits async "
                "background callables on the loop instead of threadpooling them, and the "
                "wrapper need not await anything itself, so a deliberately non-blocking "
                "enqueue stays non-blocking. See api/chat.py:_enqueue_capture_background."
            ) from exc
        if self._loop is loop and self._tasks and not all(t.done() for t in self._tasks):
            return
        self._loop = loop
        self._queue = asyncio.Queue(maxsize=capture_config.queue_maxsize())
        self._tasks = [
            loop.create_task(self._consume(index), name=f"capture-worker-{index}")
            for index in range(capture_config.worker_concurrency())
        ]
        log_event("capture.worker.started", consumers=len(self._tasks))

    async def stop(self) -> None:
        """Cancel the consumer tasks. Safe to call when never started."""
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._loop = None
        self._queue = None

    # -- producer side -----------------------------------------------------

    def enqueue(self, subject_id: str, actor_id: str, turn: Turn) -> str:
        """Hand a turn to the worker and return immediately. Never blocks.

        Deliberately a plain `def`, not `async def`: a caller physically cannot
        `await` this into the request path. Returns the job id.

        A full queue drops the job (and says so, loudly). Backpressure applied
        to the chat handler would be exactly the coupling this module exists to
        prevent -- a memory write is not worth delaying a user's reply.
        """
        self.ensure_started()
        assert self._queue is not None  # set by ensure_started

        job_id = str(uuid.uuid4())
        job = CaptureJob(
            job_id=job_id, subject_id=str(subject_id), actor_id=str(actor_id), turn=turn
        )
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            JOBS.labels(status="dropped").inc()
            log_warning("capture.worker.queue_full", job_id=job_id, subject_id=subject_id)
            return job_id

        QUEUE_DEPTH.set(self._queue.qsize())
        log_event("capture.worker.enqueued", job_id=job_id, subject_id=subject_id,
                  depth=self._queue.qsize())
        return job_id

    # -- consumer side -----------------------------------------------------

    async def _consume(self, index: int) -> None:
        assert self._queue is not None
        while True:
            job = await self._queue.get()
            try:
                await self._run_job(job)
            finally:
                self._queue.task_done()
                QUEUE_DEPTH.set(self._queue.qsize())

    async def _run_job(self, job: CaptureJob) -> None:
        """Run one job. Never raises -- a bad turn must not kill the consumer."""
        job_id = job["job_id"]
        record: dict[str, Any] = {"job_id": job_id, "subject_id": job["subject_id"]}
        try:
            final: CaptureState = await run_capture(
                job["subject_id"], job["actor_id"], job["turn"]
            )
            record["status"] = "completed"
            record["write_results"] = final.get("write_results") or []
            JOBS.labels(status="completed").inc()
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            record["status"] = "timeout"
            JOBS.labels(status="timeout").inc()
            log_warning("capture.worker.timeout", job_id=job_id,
                        budget=capture_config.capture_timeout_seconds())
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = f"{type(exc).__name__}: {exc}"
            JOBS.labels(status="failed").inc()
            log_warning("capture.worker.failed", job_id=job_id,
                        error=record["error"])

        self.completed.append(record)
        if len(self.completed) > self._max_completed:
            del self.completed[: -self._max_completed]

    # -- test/ops helper ---------------------------------------------------

    async def drain(self, timeout: float = 60.0) -> bool:
        """Wait until every queued job has finished. True if it drained in time.

        `Queue.join()` returns once `task_done()` has been called for every
        enqueued item, so this covers in-flight jobs and not just the backlog.
        """
        if self._queue is None:
            return True
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @property
    def depth(self) -> int:
        return self._queue.qsize() if self._queue is not None else 0


_worker: Optional[CaptureWorker] = None


def get_worker() -> CaptureWorker:
    """The process-wide capture worker, started against the running loop."""
    global _worker
    if _worker is None:
        _worker = CaptureWorker()
    _worker.ensure_started()
    return _worker


async def reset_worker() -> None:
    """Tear the singleton down. Used by test fixtures between event loops."""
    global _worker
    if _worker is not None:
        await _worker.stop()
    _worker = None
