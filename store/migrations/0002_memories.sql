-- 0002_memories.sql — the core `memories` table.
--
-- ${EMBEDDING_DIM} is substituted by store/migrate.py from the EMBEDDING_DIM
-- environment variable at migration time. The vector width is deliberately NOT
-- hardcoded here: swapping voyage-3.5 for a different-width embedder must be an
-- env change plus a re-embed, never a hand-edited migration file.
--
-- SCHEMA SEAM (plan step 8) --------------------------------------------------
-- Two identity columns, not one `user_id`:
--
--   subject_id : WHOSE memory this is — the person the fact is about.
--   actor_id   : WHO wrote or read it — the agent/session performing the write.
--
-- In this single-user assistant the two are always equal. The split is a
-- forward-compatible seam: a future "agent writes about a user" model, or a
-- shared-assistant model, needs no table rewrite and no data migration —
-- only new values in a column that already exists. Every RLS policy in
-- 0004_rls.sql is scoped on BOTH columns, so the seam is enforced from day one
-- rather than retrofitted.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS memories (
    id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- schema seam: see header
    subject_id           uuid        NOT NULL,
    actor_id             uuid        NOT NULL,

    content              text        NOT NULL,
    content_tsv          tsvector    GENERATED ALWAYS AS
                                     (to_tsvector('english', coalesce(content, ''))) STORED,
    embedding            vector(${EMBEDDING_DIM}),

    source               text,
    importance           real,
    confidence           real,
    weight               real        NOT NULL DEFAULT 1.0,
    reinforcement_count  integer     NOT NULL DEFAULT 0,

    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    last_accessed_at     timestamptz NOT NULL DEFAULT now(),
    deleted_at           timestamptz NULL
);

COMMENT ON TABLE  memories             IS 'Atomic remembered facts. RLS-scoped on (subject_id, actor_id).';
COMMENT ON COLUMN memories.subject_id  IS 'Schema seam: whose memory this is.';
COMMENT ON COLUMN memories.actor_id    IS 'Schema seam: who wrote or read it. Equal to subject_id in single-user mode.';
COMMENT ON COLUMN memories.embedding   IS 'Width templated from EMBEDDING_DIM at migration time.';
COMMENT ON COLUMN memories.deleted_at  IS 'Soft delete marker (M7). NULL means live.';
