-- 0003_indexes.sql — indexes backing the M3 hybrid retrieval paths.
--
--   HNSW on embedding      -> the semantic path (cosine distance, `<=>`).
--   GIN  on content_tsv    -> the keyword path (`content_tsv @@ tsquery`).
--   btree(subject_id, deleted_at) -> the per-subject live-rows filter that both
--                            paths and the M7 curated view apply.
--
-- Idempotent: CREATE INDEX IF NOT EXISTS on all three.

CREATE INDEX IF NOT EXISTS memories_embedding_hnsw_idx
    ON memories
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS memories_content_tsv_gin_idx
    ON memories
    USING gin (content_tsv);

CREATE INDEX IF NOT EXISTS memories_subject_deleted_idx
    ON memories (subject_id, deleted_at);
