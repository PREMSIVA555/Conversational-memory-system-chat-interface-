-- 0009_preference_slots.sql — M9 item 2: entity-slot preferences and supersession
--
-- WHY THIS EXISTS
-- ---------------
-- The store was an append-only log of everything the user had ever said, with no
-- way to express that one fact REPLACED another. Diagnosed from real data: a
-- user said they preferred Python, then Java, then C++, and all three sat in the
-- table as equally-true, equally-live rows. Retrieval dutifully handed the model
-- all three and it answered in whichever it liked.
--
-- Nothing about a sentence's shape says whether it supersedes another. "I prefer
-- C++" replaces "I prefer Java"; "I like dosa" does NOT replace "I like idli".
-- The difference is whether the underlying attribute holds ONE value or many —
-- a person has one default programming language and many foods they enjoy.
--
-- So a memory may now carry an `attribute`: the slot it fills, e.g.
-- `default_programming_language`. `capture/attributes.py` holds the closed list
-- of slots and which of them are single-valued. When a new memory fills a
-- single-valued slot, the previous occupant is marked superseded rather than
-- deleted.
--
-- SUPERSEDED IS NOT DELETED, and the distinction is deliberate:
--
--   deleted_at     the user asked for erasure. Gone from every read path, kept
--                  only for the GDPR export, which marks it as deleted.
--   superseded_at  the user changed their mind. Excluded from RETRIEVAL, so it
--                  never reaches the model — but still visible in the curated
--                  list, because "you told me Java in September and C++ in
--                  October" is history the user is entitled to see, and
--                  silently vanishing it would be its own kind of lying.
--
-- Idempotent: `store/migrate.py` re-runs every file on every invocation and the
-- ledger does not skip them, so every statement here is IF NOT EXISTS or guarded.

-- ---------------------------------------------------------------------------
-- 1. columns
-- ---------------------------------------------------------------------------
ALTER TABLE memories ADD COLUMN IF NOT EXISTS attribute     text        NULL;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS superseded_at timestamptz NULL;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS superseded_by uuid        NULL;

DO $fk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'memories_superseded_by_fkey'
           AND conrelid = 'memories'::regclass
    ) THEN
        ALTER TABLE memories
            ADD CONSTRAINT memories_superseded_by_fkey
            FOREIGN KEY (superseded_by) REFERENCES memories(id) ON DELETE SET NULL;
    END IF;
END
$fk$;

-- Both-or-neither. A row with a `superseded_by` and no `superseded_at` would be
-- invisible to every read filter below while still claiming to be superseded,
-- which is precisely the sort of half-state that produces a bug nobody can
-- reproduce.
DO $ck$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'memories_superseded_both_or_neither'
           AND conrelid = 'memories'::regclass
    ) THEN
        ALTER TABLE memories
            ADD CONSTRAINT memories_superseded_both_or_neither
            CHECK ((superseded_at IS NULL) = (superseded_by IS NULL));
    END IF;
END
$ck$;

-- ---------------------------------------------------------------------------
-- 2. indexes
-- ---------------------------------------------------------------------------

-- The supersession lookup: "what currently fills this slot for this subject?"
-- Partial, because `attribute` is NULL on the overwhelming majority of rows —
-- only slot-shaped facts carry one.
CREATE INDEX IF NOT EXISTS memories_attribute_current_idx
    ON memories (subject_id, attribute)
    WHERE attribute IS NOT NULL
      AND deleted_at IS NULL
      AND superseded_at IS NULL;

-- Walking a slot's history backwards, for the panel and for debugging.
CREATE INDEX IF NOT EXISTS memories_superseded_by_idx
    ON memories (superseded_by)
    WHERE superseded_by IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. documentation that survives in the database itself
-- ---------------------------------------------------------------------------
COMMENT ON COLUMN memories.attribute IS
    'The slot this memory fills, e.g. default_programming_language. NULL for '
    'facts that are not slot-shaped. Values come from the closed list in '
    'capture/attributes.py; single-valued slots trigger supersession on write.';

COMMENT ON COLUMN memories.superseded_at IS
    'When a newer memory replaced this one in a single-valued slot. Excluded '
    'from retrieval so it never reaches the model, but still returned by the '
    'curated list and the GDPR export: the user changed their mind, they did '
    'not ask for erasure. Distinct from deleted_at.';

COMMENT ON COLUMN memories.superseded_by IS
    'The memory that replaced this one. ON DELETE SET NULL, so hard-deleting '
    'the replacement does not cascade; the row simply stops naming a successor.';
