-- 0001_extensions.sql — required PostgreSQL extensions.
--
-- vector   : pgvector, supplies the `vector` type and the HNSW index method.
-- pg_trgm  : trigram similarity, used for fuzzy/keyword assistance later on.
--
-- Idempotent: IF NOT EXISTS on both.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
