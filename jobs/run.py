"""CLI entry point for the background jobs (M8 step 11).

    python -m jobs.run --job decay
    python -m jobs.run --job reflection
    python -m jobs.run --job scheduler

Two audiences, and the second one is the reason this file is a *process* rather
than a function someone imports:

1. An operator running a sweep on demand, or a cron/systemd unit that would
   rather own its own scheduling than run APScheduler.

2. `tests/distributed/`, which spawns **three real OS processes** of
   `--job decay` against one database. Three threads in one interpreter would
   not test what needs testing: `FOR UPDATE SKIP LOCKED` arbitrates between
   *transactions on separate connections*, and threads sharing a pool (or,
   worse, a connection) can produce a green test against a claim query with no
   locking in it at all. Separate processes make the concurrency real.

WHAT THE DISTRIBUTED TEST NEEDS FROM THIS FILE
----------------------------------------------
`--run-id`      every worker in one sweep must share it, or each process
                allocates its own and they all claim every row.
`--worker`      a label, echoed into every log line beside the pid so the DoD's
                "confirm three worker processes actually ran" is answerable from
                the output.
`--subject`     scope the sweep to the fixture's subjects, so a test run on a
                shared development database cannot age unrelated rows. Repeat
                for several. Production passes none and the sweep is global.
`--stats-out`   write this worker's `RunStats` — crucially including
                `processed_ids` — to a JSON file. Counts alone could not prove
                "the union of the three workers' id sets is the whole fixture
                and the pairwise intersections are empty"; identities can.

EXIT CODES
    0  the job ran to completion
    1  the job raised

stdout carries the structured log lines and then the `RunStats` JSON, so a
caller can either parse the file or scrape the output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# `python -m jobs.run` from the repo root already has the root on sys.path, but
# a subprocess spawned with a different cwd may not. Belt and braces, and it
# costs one path check.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from store.db import close_pools, ensure_selector_event_loop_policy  # noqa: E402

# Before any event loop exists: psycopg's async driver cannot run on Windows'
# default ProactorEventLoop. Same call, same reason, as `conftest.py` and
# `api/main.py`.
ensure_selector_event_loop_policy()

from jobs.metrics import RunStats, configure_logging  # noqa: E402

JOBS = ("decay", "reflection", "scheduler")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jobs.run",
        description="Run a memory-system maintenance job on demand.",
    )
    parser.add_argument("--job", required=True, choices=JOBS, help="which job to run")
    parser.add_argument("--run-id", default=None, help="share this across co-workers")
    parser.add_argument("--worker", default="worker-0", help="label for the log lines")
    parser.add_argument("--batch-size", type=int, default=None, help="rows per claim")
    parser.add_argument(
        "--subject",
        action="append",
        default=None,
        dest="subjects",
        help="restrict to this subject_id (repeatable). Omit for a global sweep.",
    )
    parser.add_argument("--actor", default=None, help="actor_id for a reflection run")
    parser.add_argument(
        "--stats-out", type=Path, default=None, help="write RunStats JSON here"
    )
    parser.add_argument(
        "--no-persist-schedule",
        action="store_true",
        help="scheduler only: use an in-memory jobstore",
    )
    parser.add_argument(
        "--wait-for",
        type=Path,
        default=None,
        help=(
            "TEST SUPPORT. Block until this file exists before claiming anything. "
            "tests/distributed uses it as a start barrier so all N workers begin "
            "within milliseconds of each other; without it the first process to "
            "finish importing can drain the whole table before the others are up, "
            "and the concurrency the test exists to exercise never happens."
        ),
    )
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=60.0,
        help="give up waiting on --wait-for after this many seconds",
    )
    return parser


async def _await_barrier(path: Path, timeout: float) -> None:
    """Poll for `path` to appear. Bounded, so a broken caller cannot wedge a worker."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not path.exists():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(
                f"--wait-for {path} never appeared within {timeout:.0f}s"
            )
        await asyncio.sleep(0.01)


async def _run_decay(args: argparse.Namespace) -> RunStats:
    from jobs.decay import run_decay_worker

    return await run_decay_worker(
        run_id=args.run_id,
        worker=args.worker,
        size=args.batch_size,
        subject_ids=args.subjects,
    )


async def _run_reflection(args: argparse.Namespace) -> RunStats:
    from jobs.reflection import run_reflection_worker

    subject = args.subjects[0] if args.subjects else None
    return await run_reflection_worker(
        subject_id=subject,
        actor_id=args.actor,
        worker=args.worker,
        run_id=args.run_id,
    )


async def _main(args: argparse.Namespace) -> int:
    configure_logging()
    try:
        if args.wait_for is not None:
            await _await_barrier(args.wait_for, args.wait_timeout)

        if args.job == "scheduler":  # pragma: no cover - a daemon
            from jobs.scheduler import run_forever

            await run_forever(persistent=not args.no_persist_schedule)
            return 0

        stats = await _run_decay(args) if args.job == "decay" else await _run_reflection(args)

        payload = stats.to_json()
        if args.stats_out:
            args.stats_out.parent.mkdir(parents=True, exist_ok=True)
            args.stats_out.write_text(payload, encoding="utf-8")
        print(payload)
        return 0
    finally:
        # Always: a worker that leaves its pool open holds connections after the
        # process' work is done, and three of those against a max_size-10 pool
        # is a PoolTimeout on the next test.
        await close_pools()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except Exception as exc:  # noqa: BLE001 - a failed job must exit non-zero
        print(f"JOB FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        # Still emit a machine-readable record, so a caller that reads
        # `--stats-out` gets "it failed" rather than a missing file it has to
        # guess about.
        if getattr(args, "stats_out", None):
            try:
                args.stats_out.parent.mkdir(parents=True, exist_ok=True)
                args.stats_out.write_text(
                    json.dumps({"job": args.job, "worker": args.worker,
                                "error": f"{type(exc).__name__}: {exc}"}, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
