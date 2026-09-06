-- 0010_summary_staleness.sql — M9 item 3: cascading invalidation for summaries
--
-- WHY THIS EXISTS
-- ---------------
-- M7 promises erasure. M8 added a reflection agent that consolidates raw
-- memories into summaries. Nobody joined the two, and the gap was found the
-- hard way by a user:
--
--   1. they stated two preferences, Java and Python
--   2. the nightly reflection agent folded them into one summary:
--      "PremSiva, who works at TCS, is learning AI engineering, has travel
--       experience, prefers Java but also likes receiving code in Python."
--   3. they DELETED both preferences. Both rows got `deleted_at` correctly.
--   4. the summary was untouched, still live, still retrievable — so the
--      assistant kept answering in Java and Python and citing the preferences
--
-- They deleted the two sentences. They never deleted the paragraph that quotes
-- them. Every read filter passed the summary because it is an ordinary live row.
--
-- That is a hole in an erasure feature, which is the worst place to have one:
-- the delete APPEARED to work, the audit trail said it worked, and the fact kept
-- reaching the model anyway.
--
-- THE EDGE ALREADY EXISTED. `0007_decay_columns.sql` gave every consolidated
-- source a `consolidated_into` pointer at its summary, and an index on it.
-- Nothing ever read that column back. This migration adds the missing state so
-- the edge can be walked when a source changes.
--
-- WHY `stale_at` AND NOT AN IMMEDIATE DELETE
-- ------------------------------------------
-- Deleting the summary outright is simpler and was considered. It loses the
-- other facts folded into it — TCS, AI engineering, travel — which are still
-- true and still the user's. Marking it stale removes it from RETRIEVAL at once
-- (the safety property: it may quote something erased) while leaving the
-- reflection job to rebuild a clean summary from whatever sources survive.
--
-- Rewriting the summary in place with an LLM was also considered and rejected:
-- a model asked to omit a fact will sometimes paraphrase it instead, and there
-- is no way to verify the omission. For an erasure path that is unacceptable.
--
-- Idempotent: `store/migrate.py` re-runs every file on every invocation.

-- ---------------------------------------------------------------------------
-- 1. column
-- ---------------------------------------------------------------------------
ALTER TABLE memories ADD COLUMN IF NOT EXISTS stale_at timestamptz NULL;

-- ---------------------------------------------------------------------------
-- 2. index
-- ---------------------------------------------------------------------------

-- The rebuild queue: "which summaries need regenerating?". Partial, because
-- staleness is rare and only ever applies to `source='reflection'` rows.
CREATE INDEX IF NOT EXISTS memories_stale_idx
    ON memories (subject_id, stale_at)
    WHERE stale_at IS NOT NULL
      AND deleted_at IS NULL;

-- ---------------------------------------------------------------------------
-- 3. documentation that survives in the database itself
-- ---------------------------------------------------------------------------
COMMENT ON COLUMN memories.stale_at IS
    'Set on a reflection summary when one of its source memories was deleted, '
    'edited or superseded, so the summary may now assert something that is no '
    'longer true. Excluded from retrieval immediately; rebuilt by the next '
    'reflection run, which soft-deletes it and frees its surviving sources to '
    'be consolidated again. NULL on every non-summary row.';
