"""Fixtures for the distributed decay tests (M8 step 16).

Two things live here:

* `decay_fixture` — a known number of rows under a fresh, isolated subject id,
  with staggered `last_accessed_at` values so the claim query's
  `ORDER BY last_accessed_at` has something real to order. Purged at teardown.

* `spawn_workers` — starts N **real OS processes** of `python -m jobs.run --job
  decay` against the shared database, releases them all at once, and returns
  each one's parsed `RunStats`.

WHY SUBPROCESSES AND NOT THREADS
--------------------------------
`FOR UPDATE SKIP LOCKED` arbitrates between *transactions on separate database
connections*. Threads inside one interpreter share a connection pool, and the
convenient way to write such a test — one pool, N tasks — can easily end up
running every "worker" on the same connection, where row locks are re-entrant
and SKIP LOCKED has nothing to skip. That test passes against a claim query with
the locking removed entirely, which makes it worse than no test.

Separate processes have separate pools, separate connections and separate
backends. Nothing about the concurrency is simulated.

WHY THERE IS A START BARRIER
----------------------------
Python startup plus a LangGraph import is a second or two, and it is not the
same second or two in every process. Without a barrier the first worker to
finish importing can drain the whole fixture before the second one has opened a
connection — the test would still *pass* (one worker processing everything
exactly once is a correct outcome) while exercising none of the concurrency it
exists to test. It would be exactly the shape of green-but-empty test this
project has been bitten by before.

So every worker is started with `--wait-for <barrier>`; the fixture waits until
all N processes are alive, then creates the file, and all N begin claiming
within milliseconds of each other. Combined with `--batch-size 1` over a few
hundred rows, the drain takes long enough that overlap is structural rather than
lucky — and `test_decay_claims_no_double_process` asserts that at least two
workers actually did claim rows, so a regression back to "one worker takes
everything" fails rather than passes quietly.

NOTE ON THE AUTOUSE POOL FIXTURE
--------------------------------
`tests/conftest.py` already provides `_isolate_loop_bound_resources`, and this
directory inherits it. Do not add a copy here — two autouse copies would close
the pools twice per test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import pytest

from store.db import admin_session, ensure_selector_event_loop_policy, load_env

ensure_selector_event_loop_policy()
load_env()

ROOT = Path(__file__).resolve().parents[2]

#: Rows in the fixture table. Large enough that a single worker draining it at
#: `--batch-size 1` takes on the order of a second — comfortably longer than the
#: spread in process start times — so several workers genuinely overlap.
DEFAULT_ROW_COUNT = 300

#: One row per claim. The point is not throughput; it is that a worker holds its
#: locks for the shortest possible time and releases them many times, which is
#: what gives the other workers something to interleave with.
DEFAULT_BATCH_SIZE = 1

#: Hard ceiling on one worker subprocess. Generous relative to the ~2-5s a run
#: actually takes, but bounded: a wedged worker must fail the test rather than
#: hang the suite.
WORKER_TIMEOUT = 180.0


@dataclass
class DecayFixture:
    """A known set of rows under one isolated subject."""

    subject_id: str
    run_id: str
    memory_ids: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.memory_ids)

    @property
    def id_set(self) -> set[str]:
        return set(self.memory_ids)


@dataclass
class WorkerResult:
    """One subprocess: how it exited, what it logged, what it processed."""

    worker: str
    returncode: int
    stdout: str
    stderr: str
    stats: dict[str, Any]

    @property
    def output(self) -> str:
        """stdout and stderr together.

        `jobs/metrics.py:configure_logging()` attaches a `logging.StreamHandler`,
        which writes to **stderr** by convention, while the `RunStats` JSON is
        printed to stdout. Both are this worker's output and assertions about
        "what the worker logged" should not have to know which pipe a given line
        came down.
        """
        return self.stdout + "\n" + self.stderr

    @property
    def pid(self) -> int:
        return int(self.stats.get("pid", -1))

    @property
    def processed_ids(self) -> list[str]:
        return [str(i) for i in self.stats.get("processed_ids", [])]

    @property
    def processed_set(self) -> set[str]:
        return set(self.processed_ids)


_SEED_SQL = """
INSERT INTO memories
       (subject_id, actor_id, content, weight, reinforcement_count, last_accessed_at)
SELECT %(subject_id)s::uuid,
       %(subject_id)s::uuid,
       'decay fixture row ' || g::text,
       1.0,
       (g %% 4),
       now() - make_interval(days => (g %% 400))
  FROM generate_series(1, %(n)s) AS g
RETURNING id
"""


@pytest.fixture
async def decay_fixture() -> DecayFixture:
    """Seed `DEFAULT_ROW_COUNT` rows under a fresh subject. Purged at teardown.

    Written on the ADMIN connection, matching the connection the decay job
    itself uses (see `jobs/claims.py` on why the sweep is an owner-connection
    job). The rows carry no embedding — decay never reads one, and embedding
    300 rows would cost 100 minutes of Voyage rate-limit backoff for data
    nothing under test looks at.

    `reinforcement_count` cycles 0-3 and ages cycle over 400 days so the fixture
    exercises the damping term and straddles `ARCHIVE_THRESHOLD` in both
    directions, rather than being 300 identical rows.
    """
    subject_id = str(uuid.uuid4())
    async with admin_session() as conn:
        cursor = await conn.execute(
            _SEED_SQL, {"subject_id": subject_id, "n": DEFAULT_ROW_COUNT}
        )
        ids = [str(row["id"]) for row in await cursor.fetchall()]

    fixture = DecayFixture(
        subject_id=subject_id, run_id=str(uuid.uuid4()), memory_ids=ids
    )
    try:
        yield fixture
    finally:
        async with admin_session() as conn:
            await conn.execute(
                "DELETE FROM memories WHERE subject_id = %s::uuid", (subject_id,)
            )


def spawn_workers(
    *,
    count: int,
    run_id: str,
    subject_ids: Sequence[str],
    out_dir: Path,
    batch_size: int = DEFAULT_BATCH_SIZE,
    timeout: float = WORKER_TIMEOUT,
) -> list[WorkerResult]:
    """Start `count` real `python -m jobs.run --job decay` processes at once.

    Returns one `WorkerResult` per process once all have exited. Raises if any
    worker exits non-zero or fails to write its stats file — a worker that
    crashed must not be silently counted as "processed nothing".
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    barrier = out_dir / "start.barrier"
    if barrier.exists():  # pragma: no cover - fresh tmp_path every test
        barrier.unlink()

    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        # The runner prints structured JSON log lines; a cp1252 console would
        # raise UnicodeEncodeError on a stray non-ASCII byte and fail the run
        # for a cosmetic reason. Same guard `test_eval_harness.py` uses.
        "PYTHONIOENCODING": "utf-8",
        # Unbuffered, so a worker that hangs still has its log lines on the pipe.
        "PYTHONUNBUFFERED": "1",
    }

    subject_args: list[str] = []
    for subject_id in subject_ids:
        subject_args += ["--subject", str(subject_id)]

    procs: list[tuple[str, Path, subprocess.Popen]] = []
    for index in range(count):
        worker = f"worker-{index}"
        stats_path = out_dir / f"{worker}.json"
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "jobs.run",
                "--job", "decay",
                "--run-id", str(run_id),
                "--worker", worker,
                "--batch-size", str(batch_size),
                "--stats-out", str(stats_path),
                "--wait-for", str(barrier),
                "--wait-timeout", str(timeout),
                *subject_args,
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        procs.append((worker, stats_path, proc))

    # Release them together. A short settle first so every process is past
    # `import langgraph` and sitting in the barrier poll rather than still
    # starting up — otherwise the barrier is created before some workers exist
    # and the whole point of it is lost.
    time.sleep(2.0)
    barrier.write_text("go", encoding="utf-8")

    results: list[WorkerResult] = []
    for worker, stats_path, proc in procs:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:  # pragma: no cover - the bound
            proc.kill()
            stdout, stderr = proc.communicate()
            raise AssertionError(
                f"{worker} did not finish within {timeout}s. A worker that blocks "
                "rather than skipping is the signature of a claim query that has "
                "lost SKIP LOCKED.\n--- stdout ---\n" + stdout + "\n--- stderr ---\n" + stderr
            )

        assert proc.returncode == 0, (
            f"{worker} exited {proc.returncode}\n--- stdout ---\n{stdout}\n"
            f"--- stderr ---\n{stderr}"
        )
        assert stats_path.exists(), (
            f"{worker} wrote no stats file\n--- stdout ---\n{stdout}\n"
            f"--- stderr ---\n{stderr}"
        )
        results.append(
            WorkerResult(
                worker=worker,
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                stats=json.loads(stats_path.read_text(encoding="utf-8")),
            )
        )

    return results


@pytest.fixture
def spawn_decay_workers():
    """`spawn_workers` as a fixture.

    Handed over as a fixture rather than imported by the test module because
    `tests/` deliberately carries no `__init__.py` — pytest imports each test
    file as a top-level module, so `from .conftest import spawn_workers` raises
    `ImportError: attempted relative import with no known parent package`. A
    fixture is the sanctioned way for a conftest to export a callable.
    """
    return spawn_workers
