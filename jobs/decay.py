"""Decay: the pure weight function, the archive rule, and the drain loop.

M8 steps 3, 4 and 6.

THE ONE DESIGN DECISION THAT MATTERS: DECAY IS IDEMPOTENT
=========================================================
The obvious implementation is multiplicative — read `weight`, multiply it by a
per-run factor, write it back. It is also wrong, and it is wrong in a way that
would not show up in any single-run test:

    a multiplicative job is not idempotent. Run it twice on the same night —
    a retry after a crash, two overlapping cron fires, an operator running
    `python -m jobs.run --job decay` by hand after the scheduler already did —
    and every weight in the table has been halved twice. Nothing errors.
    Nothing logs. The store is just quietly wrong, permanently, because there
    is no record of what the weight *should* have been.

So `decay_weight()` is a **pure function of the row's age**, not of its current
weight:

    weight = peak(reinforcement_count) * 0.5 ** (age_days / effective_half_life)

`age_days` is measured from `last_accessed_at`, which `store/memories.py:
reinforce()` already refreshes on every reinforcement and which M4's recency
feature already treats as the canonical "when was this last relevant" signal.
Running the job ten times in a row produces exactly the same number as running
it once. A crashed run can simply be re-run. That property is worth more than
the small extra arithmetic.

`peak()` is the weight the row would have if it had just been accessed, which is
`1.0` plus M2's reinforcement bonus, capped by M2's ceiling — read from
`capture/config.py` rather than re-spelled here, so the decay job and the
capture writer cannot disagree about what a reinforced row is worth.

THE DAMPING TERM
----------------
`effective_half_life = HALF_LIFE_DAYS * (1 + reinforcement_count / DAMPING_REFERENCE)`

A fact the user has restated several times is a fact about them, not a passing
remark, and it should survive a quiet month. The damping is on the *half-life*
rather than on the resulting weight because that is the shape that means "it
decays more slowly" rather than "it starts higher" — the two are different
claims and only the first is what reinforcement should buy. `peak()` already
handles "it starts higher". `test_reinforced_memory_decays_slower` pins the
damping specifically, holding `base_weight` equal so that the higher peak cannot
carry the assertion on its own.

THE FLOOR
---------
Weight bottoms out at `DECAY_FLOOR` rather than at zero. A zero weight would
zero out M4's whole `0.4 * semantic + ...` score for that row regardless of how
well it matched the query, which turns aging into deletion by arithmetic. The
floor keeps an old memory retrievable when it is genuinely the best answer,
just ranked below anything fresher.

ARCHIVING
---------
`archive_row()` sets `archived_at` once weight falls below `ARCHIVE_THRESHOLD`.
The threshold sits ABOVE the floor on purpose — a row pinned at the floor is by
definition one nothing has touched in a very long time, so if the threshold were
at or below the floor nothing would ever archive.

Archiving must never resurrect or un-delete a soft-deleted row. Two independent
guards, because this is a governance property and one guard is a convention:

  1. the claim predicate in `jobs/claims.py` never returns a row with a
     non-NULL `deleted_at`, so the decay path cannot reach one; and
  2. every UPDATE in this module carries `AND deleted_at IS NULL` anyway.

Neither statement in this module mentions `deleted_at` on the SET side at all,
so there is no code path that can clear it.
"""

from __future__ import annotations

import asyncio
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from capture import config as capture_config
from jobs.claims import ClaimedRow, claim_session, new_run_id
from jobs.metrics import RunStats, log_event

__all__ = [
    "DECAY_FLOOR",
    "ARCHIVE_THRESHOLD",
    "half_life_days",
    "damping_reference",
    "decay_floor",
    "archive_threshold",
    "batch_size",
    "peak_weight",
    "age_in_days",
    "decay_weight",
    "WeightUpdate",
    "compute_updates",
    "apply_weights",
    "archive_row",
    "archive_rows",
    "run_decay_worker",
]


# ---------------------------------------------------------------------------
# tunables — env-overridable, resolved at call time (never frozen at import)
# ---------------------------------------------------------------------------

DEFAULTS = {
    # Half-life of an un-reinforced memory, in days. Deliberately the same 30
    # days as M4's `RECENCY_HALF_LIFE_DAYS`: the ranker's recency feature and
    # the decay job are two views of the same claim — "a stated preference stops
    # being a safe assumption after about a month" — and having them disagree
    # would mean the store ages rows on one schedule and ranks them on another.
    "DECAY_HALF_LIFE_DAYS": "30.0",
    # Reinforcements needed to double the half-life. 3 matches M4's
    # `FREQUENCY_REFERENCE`, for the same reason.
    "DECAY_DAMPING_REFERENCE": "3.0",
    # Lowest weight decay will produce. See THE FLOOR above.
    "DECAY_FLOOR": "0.05",
    # Weight below which a row is archived. Must be > DECAY_FLOOR or nothing
    # ever archives — asserted at the bottom of this module.
    "DECAY_ARCHIVE_THRESHOLD": "0.10",
    # Rows per claim. Small enough that a worker holds its locks briefly and
    # three workers interleave visibly; large enough that a big table does not
    # cost one transaction per row.
    "DECAY_BATCH_SIZE": "50",
    # Safety rail on the drain loop, so a bug that stops marking rows cannot
    # spin forever against production.
    "DECAY_MAX_BATCHES": "10000",
}


def _env(name: str) -> str:
    from store.db import load_env

    load_env()
    value = os.environ.get(name)
    return DEFAULTS[name] if value is None or value == "" else value


def half_life_days() -> float:
    return float(_env("DECAY_HALF_LIFE_DAYS"))


def damping_reference() -> float:
    return max(1e-9, float(_env("DECAY_DAMPING_REFERENCE")))


def decay_floor() -> float:
    return float(_env("DECAY_FLOOR"))


def archive_threshold() -> float:
    return float(_env("DECAY_ARCHIVE_THRESHOLD"))


def batch_size() -> int:
    return max(1, int(_env("DECAY_BATCH_SIZE")))


def max_batches() -> int:
    return max(1, int(_env("DECAY_MAX_BATCHES")))


#: Module-level aliases for readers who want the value rather than the lookup.
#: Call the functions in code — these are snapshots taken at import.
DECAY_FLOOR = decay_floor()
ARCHIVE_THRESHOLD = archive_threshold()


# ---------------------------------------------------------------------------
# step 3 — the pure decay function
# ---------------------------------------------------------------------------

def peak_weight(reinforcement_count: int) -> float:
    """The weight this row would carry if it had just been accessed.

    1.0 (the `memories.weight` column default) plus M2's reinforcement bonus,
    capped by M2's ceiling. Both constants come from `capture/config.py` rather
    than being re-spelled here: `reinforce()` writes `LEAST(weight + inc, max)`,
    and a decay job that recomputed from a *different* increment would fight
    the capture writer every night.
    """
    increment = capture_config.weight_increment()
    ceiling = capture_config.weight_max()
    return min(ceiling, 1.0 + increment * max(0, int(reinforcement_count)))


def age_in_days(last_accessed_at: datetime, now: datetime | None = None) -> float:
    """Days since `last_accessed_at`, floored at 0.

    Floored because a clock skew between the application host and the database
    can hand back a `last_accessed_at` a few seconds in the future, and a
    negative age would *increase* the weight above its peak.
    """
    reference = now or datetime.now(timezone.utc)
    if last_accessed_at.tzinfo is None:
        last_accessed_at = last_accessed_at.replace(tzinfo=timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return max(0.0, (reference - last_accessed_at).total_seconds() / 86400.0)


def decay_weight(
    *,
    age_days: float,
    reinforcement_count: int = 0,
    base_weight: float | None = None,
    half_life: float | None = None,
    floor: float | None = None,
) -> float:
    """Exponential decay on age, damped by reinforcement, floored.

    PURE. No I/O, no clock, no globals read at call time other than the env
    tunables (which are explicit parameters when a caller wants determinism).
    Unit-testable in isolation, which is what `tests/unit/test_decay.py` does.

        weight = base * 0.5 ** (age_days / (half_life * (1 + n / DAMPING_REF)))

    `base_weight` defaults to `peak_weight(reinforcement_count)` — see the
    module docstring on why the input is the row's *peak*, never its current
    stored weight.
    """
    base = peak_weight(reinforcement_count) if base_weight is None else float(base_weight)
    hl = half_life_days() if half_life is None else float(half_life)
    bottom = decay_floor() if floor is None else float(floor)

    if hl <= 0:  # pragma: no cover - guarded by the invariant check below
        return max(bottom, base)

    effective_half_life = hl * (1.0 + max(0, int(reinforcement_count)) / damping_reference())
    decayed = base * math.pow(0.5, max(0.0, float(age_days)) / effective_half_life)
    return max(bottom, decayed)


# ---------------------------------------------------------------------------
# step 4 — applying the result
# ---------------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class WeightUpdate:
    """One row's computed new weight, and whether that weight retires it."""

    id: str
    old_weight: float
    new_weight: float
    should_archive: bool


def compute_updates(
    rows: Sequence[ClaimedRow],
    *,
    now: datetime | None = None,
    threshold: float | None = None,
) -> list[WeightUpdate]:
    """Pure: claimed rows in, computed updates out. No database access."""
    cutoff = archive_threshold() if threshold is None else float(threshold)
    reference = now or datetime.now(timezone.utc)
    updates: list[WeightUpdate] = []
    for row in rows:
        new_weight = decay_weight(
            age_days=age_in_days(row.last_accessed_at, reference),
            reinforcement_count=row.reinforcement_count,
        )
        updates.append(
            WeightUpdate(
                id=row.id,
                old_weight=row.weight,
                new_weight=new_weight,
                should_archive=new_weight < cutoff,
            )
        )
    return updates


_APPLY_SQL = """
UPDATE memories AS m
   SET weight = v.new_weight
  FROM unnest(%(ids)s::uuid[], %(weights)s::real[]) AS v(id, new_weight)
 WHERE m.id = v.id
   AND m.deleted_at IS NULL
RETURNING m.id
"""


async def apply_weights(conn: Any, updates: Sequence[WeightUpdate]) -> list[str]:
    """Write every new weight in ONE statement. Returns the ids actually updated.

    One statement rather than a loop: a batch of 50 rows is 50 network
    round-trips otherwise, all inside a transaction holding 50 row locks. The
    `unnest(...)` join is the standard way to say "these ids get these values"
    without a temporary table.

    `updated_at` is deliberately NOT touched. It means "when did the *content*
    of this memory last change", and a background weight adjustment is not a
    change to the memory. Bumping it would make every row in the store look
    freshly edited after the first nightly run, which would be visible in the
    UI and in the export.

    `deleted_at` appears only in the WHERE clause and never in SET, so nothing
    here can clear it. See the module docstring's two-guard note.
    """
    if not updates:
        return []
    cursor = await conn.execute(
        _APPLY_SQL,
        {
            "ids": [u.id for u in updates],
            "weights": [float(u.new_weight) for u in updates],
        },
    )
    return [str(row["id"]) for row in await cursor.fetchall()]


_ARCHIVE_SQL = """
UPDATE memories
   SET archived_at = now()
 WHERE id = ANY(%(ids)s::uuid[])
   AND archived_at IS NULL
   AND deleted_at  IS NULL
RETURNING id
"""


async def archive_rows(conn: Any, memory_ids: Sequence[str]) -> list[str]:
    """Mark rows archived. Returns the ids whose `archived_at` this call set.

    Idempotent by construction: `archived_at IS NULL` in the WHERE means a row
    already archived is not re-stamped and is absent from the return value, so
    the count is the honest number of *new* archivals rather than the number of
    ids passed in.

    `deleted_at IS NULL` is the guard that makes archiving unable to touch a
    soft-deleted row. It is redundant with the claim predicate today — that is
    the point of having it.
    """
    if not memory_ids:
        return []
    cursor = await conn.execute(_ARCHIVE_SQL, {"ids": [str(i) for i in memory_ids]})
    return [str(row["id"]) for row in await cursor.fetchall()]


async def archive_row(conn: Any, memory_id: str) -> bool:
    """Single-row form of `archive_rows()`. True if this call archived it."""
    return bool(await archive_rows(conn, [memory_id]))


# ---------------------------------------------------------------------------
# step 6 — the drain loop
# ---------------------------------------------------------------------------

async def run_decay_worker(
    *,
    run_id: str | None = None,
    worker: str = "worker-0",
    size: int | None = None,
    subject_ids: Sequence[str] | None = None,
    now: datetime | None = None,
    stats: RunStats | None = None,
) -> RunStats:
    """Claim-process-commit until `claim_batch()` returns empty.

    THIS is what makes the job distributed rather than merely concurrent. One
    process does not scan the table and hand out work; every process runs this
    identical loop against the shared table, and `SKIP LOCKED` plus the
    `decay_run_id` stamp are the only coordination. Start one worker and it
    drains the table alone; start three and they split it, in whatever
    proportion their speeds happen to produce, with no partitioning scheme to
    get wrong and no coordinator to fall over.

    Each batch is ONE transaction: claim, compute, apply, archive, commit. A
    worker killed mid-batch rolls back its claims, so those rows lose their
    `decay_run_id` stamp and the next `claim_batch()` from any worker picks them
    up. Nothing is stranded by a crash.

    The graph in `graphs/decay_graph.py` is what runs inside each iteration;
    this function owns the transaction and the loop around it. Imported lazily
    so that `jobs.decay` stays importable (and `decay_weight` stays unit
    testable) without pulling LangGraph in.
    """
    from graphs.decay_graph import run_decay_batch

    run = str(run_id or new_run_id())
    n = size if size is not None else batch_size()
    record = stats or RunStats(job="decay", run_id=run, worker=worker)
    record.start()

    log_event(
        "decay.worker.start",
        run_id=run,
        worker=worker,
        pid=record.pid,
        batch_size=n,
        scoped_subjects=len(subject_ids) if subject_ids is not None else None,
    )

    try:
        for _ in range(max_batches()):
            async with claim_session() as conn:
                batch = await run_decay_batch(
                    conn,
                    run_id=run,
                    batch_size=n,
                    subject_ids=subject_ids,
                    now=now,
                )
            claimed_ids = batch["claimed_ids"]
            if not claimed_ids:
                break

            record.batches += 1
            record.count_claimed(len(claimed_ids))
            record.count_decayed(len(batch["decayed_ids"]))
            record.count_archived(len(batch["archived_ids"]))
            record.add_processed(claimed_ids)

            log_event(
                "decay.batch",
                run_id=run,
                worker=worker,
                pid=record.pid,
                batch=record.batches,
                claimed=len(claimed_ids),
                decayed=len(batch["decayed_ids"]),
                archived=len(batch["archived_ids"]),
                first_id=claimed_ids[0],
            )
            # Yield the loop so co-running workers in the same process (and the
            # OS scheduler for separate ones) get a fair shot at the next batch.
            await asyncio.sleep(0)
        else:  # pragma: no cover - the rail, not the path
            record.error = f"drain loop hit DECAY_MAX_BATCHES={max_batches()}"
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        record.error = f"{type(exc).__name__}: {exc}"
        record.finish(outcome="error")
        record.log()
        raise

    record.finish(outcome="ok")
    record.log()
    return record


# ---------------------------------------------------------------------------
# invariant
# ---------------------------------------------------------------------------
#
# An explicit raise rather than `assert`, because `python -O` strips asserts and
# this is the one relationship that makes archiving reachable at all.
if archive_threshold() <= decay_floor():
    raise RuntimeError(
        "DECAY_ARCHIVE_THRESHOLD must be strictly greater than DECAY_FLOOR, or no "
        f"row can ever fall below it: got threshold={archive_threshold()} "
        f"floor={decay_floor()}"
    )
