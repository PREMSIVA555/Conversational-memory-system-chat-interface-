"""Wait until asynchronous capture stops writing to the eval fixture subject.

Used by the frontend's live e2e cleanup (`frontend/e2e/live/*.spec.ts`).

WHY THIS EXISTS
---------------
The live specs share a subject with the M8 golden set, and a chat turn's capture
runs AFTER the reply completes — measured at roughly fifteen seconds later. So
re-seeding the corpus at the end of the run is not enough on its own: a capture
still in flight commits `assistant_note` rows into the golden set *after*
cleanup has already finished.

That is not hypothetical. A cold verifier measured `assistant_note | 5` written
after the final reseed, and pointed out that the clean result the previous run
reported was luck rather than structure.

So: poll the row count for the subject until it holds steady, and only then let
the caller reseed. Exits 0 whether or not it converges — a slow capture is not a
reason to fail the test run, and the reseed that follows is still correct; it
just might not be the last write. The printed line says which happened.

Run:  python scripts/e2e_settle.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from store.db import admin_session, close_pools, ensure_selector_event_loop_policy  # noqa: E402

ensure_selector_event_loop_policy()

from evals.fixtures.seed_memories import GOLDEN_SET_SUBJECT_ID  # noqa: E402

#: How long to wait for the count to stop moving before giving up.
TIMEOUT_SECONDS = 45.0
#: Consecutive identical readings that count as "settled".
STABLE_READINGS = 3
POLL_SECONDS = 2.0


async def _count() -> int:
    async with admin_session() as conn:
        cursor = await conn.execute(
            "SELECT count(*) AS n FROM memories WHERE subject_id = %s::uuid",
            (GOLDEN_SET_SUBJECT_ID,),
        )
        return int((await cursor.fetchone())["n"])


async def main() -> int:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    previous = -1
    stable = 0
    try:
        while time.monotonic() < deadline:
            current = await _count()
            stable = stable + 1 if current == previous else 0
            if stable >= STABLE_READINGS:
                print(f"capture settled at {current} rows")
                return 0
            previous = current
            await asyncio.sleep(POLL_SECONDS)
    finally:
        # A worker that leaves its pool open holds connections after its work is
        # done, and the next suite pays for it with a PoolTimeout.
        await close_pools()

    # Deliberately still exit 0: a capture that is merely slow should not fail a
    # frontend test run. The caller reseeds either way; this only means the
    # reseed may not be the last write.
    print(f"capture did not settle within {TIMEOUT_SECONDS:.0f}s (last count {previous})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
