-- 0007_decay_columns.sql — M8 step 1: lifecycle + claim bookkeeping on `memories`.
--
-- M8 makes the store self-maintaining. Two background jobs need columns that do
-- not exist yet:
--
--   the DECAY job       ages `weight` and retires rows that have fallen below
--                       ARCHIVE_THRESHOLD. It runs as several cooperating
--                       worker processes sharing one table, so it needs
--                       per-row claim bookkeeping as well as a lifecycle mark.
--
--   the REFLECTION job  consolidates a cluster of raw memories into one summary
--                       memory, and has to record which rows were folded into
--                       which summary — otherwise the same cluster is
--                       re-summarised on every run and the store grows a pile
--                       of near-identical summaries.
--
-- FILE NUMBERING -------------------------------------------------------------
-- 0006 and 0008 are M7's (audit append-only). 0007 was reserved for this file.
-- `store/migrate.py` re-applies EVERY file in filename order on every run, so on
-- a database that already has 0008 applied, the next `python -m store.migrate`
-- runs ... 0006, 0007, 0008. That is safe here because this file is completely
-- independent of those two: it touches only `memories`, never `audit_log`, and
-- never the `audit_log_append_only()` function or its trigger. Verified rather
-- than assumed — `--status` after applying shows 0007 present with 0008's
-- run_count incremented and the trigger still installed.
--
-- WHY FOUR COLUMNS AND NOT ONE ------------------------------------------------
--
--   archived_at        LIFECYCLE. Set once, by the decay job, when a row's
--                      weight falls below ARCHIVE_THRESHOLD. Deliberately
--                      distinct from `deleted_at`: archiving is the system
--                      saying "this looks stale", deletion is the *user*
--                      saying "erase this". Conflating them would let a
--                      background job produce something indistinguishable
--                      from an exercised right to erasure, and M7's whole
--                      governance story rests on `deleted_at` meaning exactly
--                      one thing.
--
--                      NOTE what this column does NOT do: nothing in
--                      `retrieve/` filters on it. An archived memory is still
--                      retrievable. That is the intended semantics for now —
--                      archiving is a lifecycle marker and a ranking input,
--                      not a visibility filter — and golden_set_v2 carries a
--                      query whose answer is an archived row precisely so that
--                      anyone who later adds an `archived_at IS NULL` filter to
--                      retrieval trips a red test instead of silently making
--                      old memories unreachable.
--
--   decay_claimed_at   BOOKKEEPING. When this row was last claimed by a decay
--                      worker. Diagnostic only — never read by the claim
--                      predicate. Its value is being able to answer "when did
--                      this row last get looked at by the job?" from psql.
--
--   decay_run_id       BOOKKEEPING, and the load-bearing one. Workers in a
--                      single decay run share one run id; a row carrying the
--                      current run id has already been claimed by *some*
--                      worker in *this* run and is therefore not eligible
--                      again. That is what lets three processes drain the same
--                      table cooperatively without a coordinator: the claim
--                      predicate is `decay_run_id IS DISTINCT FROM :run_id`,
--                      and `FOR UPDATE SKIP LOCKED` resolves the races between
--                      the read and the mark.
--
--                      IS DISTINCT FROM, not `<>`: a NULL `decay_run_id` (a row
--                      never touched by any run) must compare as eligible, and
--                      `NULL <> 'x'` is NULL, which is not true, which would
--                      make every fresh row permanently invisible to the job.
--
--   consolidated_at /  REFLECTION. Which summary a raw row was folded into, and
--   consolidated_into  when. `consolidated_into` is a self-reference to the
--                      summary memory. ON DELETE SET NULL so that deleting a
--                      summary does not cascade into the raw rows it drew from
--                      — the sources are the user's memories and outlive any
--                      derived summary.
--
-- THE INDEX -------------------------------------------------------------------
-- The claim query is
--
--     SELECT id FROM memories
--      WHERE deleted_at IS NULL AND archived_at IS NULL
--        AND decay_run_id IS DISTINCT FROM :run_id
--      ORDER BY last_accessed_at
--      LIMIT :n FOR UPDATE SKIP LOCKED
--
-- so what it needs is a cheap ordered scan of *eligible* rows. A PARTIAL index
-- on `last_accessed_at` carrying the two IS NULL predicates is exactly that: the
-- index contains only claimable rows, so it shrinks as rows are archived and the
-- ORDER BY ... LIMIT is answered by an index scan that stops after n rows rather
-- than sorting the table. The `decay_run_id` term is deliberately NOT in the
-- index predicate — it changes on every run, which would make the index
-- non-immutable and useless.
--
-- The second index is for reporting (`select count(*) ... group by decay_run_id`)
-- and is partial on NOT NULL so it costs nothing on a table nobody has decayed.
--
-- GRANTS ----------------------------------------------------------------------
-- None needed. 0004 grants SELECT/INSERT/UPDATE/DELETE at TABLE level on
-- `memories` to ${APP_DB_USER}, and table-level privileges cover columns added
-- later. The reflection job (which runs as the app role, inside RLS) updates
-- `consolidated_*`; the decay job runs as the owner — see `jobs/claims.py` for
-- why that boundary is where it is.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, CREATE INDEX IF NOT EXISTS, and a
-- guarded constraint add.

-- ---------------------------------------------------------------------------
-- 1. lifecycle + claim bookkeeping
-- ---------------------------------------------------------------------------
ALTER TABLE memories ADD COLUMN IF NOT EXISTS archived_at      timestamptz NULL;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS decay_claimed_at timestamptz NULL;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS decay_run_id     uuid        NULL;

-- ---------------------------------------------------------------------------
-- 2. reflection consolidation linkage
-- ---------------------------------------------------------------------------
ALTER TABLE memories ADD COLUMN IF NOT EXISTS consolidated_at   timestamptz NULL;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS consolidated_into uuid        NULL;

DO $fk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'memories_consolidated_into_fkey'
           AND conrelid = 'memories'::regclass
    ) THEN
        ALTER TABLE memories
            ADD CONSTRAINT memories_consolidated_into_fkey
            FOREIGN KEY (consolidated_into) REFERENCES memories(id) ON DELETE SET NULL;
    END IF;
END
$fk$;

-- ---------------------------------------------------------------------------
-- 3. indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS memories_decay_claim_idx
    ON memories (last_accessed_at)
    WHERE deleted_at IS NULL AND archived_at IS NULL;

CREATE INDEX IF NOT EXISTS memories_decay_run_idx
    ON memories (decay_run_id)
    WHERE decay_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS memories_consolidated_into_idx
    ON memories (consolidated_into)
    WHERE consolidated_into IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 4. documentation that survives in the database itself
-- ---------------------------------------------------------------------------
COMMENT ON COLUMN memories.archived_at       IS
    'M8: set once by the decay job when weight falls below ARCHIVE_THRESHOLD. '
    'Lifecycle marker, NOT a visibility filter and NOT a deletion — retrieval '
    'does not filter on it and `deleted_at` remains the only erasure signal.';
COMMENT ON COLUMN memories.decay_claimed_at  IS
    'M8: when this row was last claimed by a decay worker. Diagnostic only.';
COMMENT ON COLUMN memories.decay_run_id      IS
    'M8: the decay run that last claimed this row. The claim predicate is '
    '`decay_run_id IS DISTINCT FROM :run_id`, which is what lets N workers '
    'drain one table cooperatively.';
COMMENT ON COLUMN memories.consolidated_at   IS
    'M8: when this raw memory was folded into a reflection summary.';
COMMENT ON COLUMN memories.consolidated_into IS
    'M8: the reflection summary memory this raw row was folded into.';
