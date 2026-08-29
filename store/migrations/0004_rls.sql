-- 0004_rls.sql — row-level security on `memories`, plus the non-superuser role
-- that makes RLS actually mean something.
--
-- WHY A SEPARATE ROLE ---------------------------------------------------------
-- PostgreSQL superusers bypass row-level security unconditionally, and a table's
-- owner bypasses it unless FORCE ROW LEVEL SECURITY is set. If the application
-- connected as POSTGRES_USER (the owner/superuser that runs these migrations),
-- every policy below would be dead code and `test_rls_blocks_cross_subject_read`
-- would pass rows straight through. So:
--
--   * ${APP_DB_USER} is created here as a plain LOGIN role — NOSUPERUSER,
--     NOCREATEDB, NOCREATEROLE, and NOT the owner of any table.
--   * It is granted only DML on the three application tables.
--   * store/db.py connects the application pool as this role (APP_DATABASE_URL,
--     or DATABASE_URL with the credentials swapped).
--   * FORCE ROW LEVEL SECURITY is set as well, so even the owner is subject to
--     the policies — defence in depth.
--
-- THE PREDICATE ---------------------------------------------------------------
-- Every policy is scoped on BOTH seam columns:
--
--   subject_id = current_setting('app.subject_id')  AND
--   actor_id   = current_setting('app.actor_id')
--
-- The GUCs are set per transaction by store/db.py:session(). When they are
-- unset, current_setting(..., true) returns NULL, the comparison is NULL, and
-- the row is filtered out — RLS fails closed, never open.
--
-- NOTE for the Definition-of-Done query
--   `select tablename, qual from pg_policies where tablename='memories'`
-- shows qual = NULL for the INSERT policy. That is a PostgreSQL rule, not an
-- omission: an INSERT policy may only carry WITH CHECK, never USING. Use
--   select tablename, cmd, coalesce(qual, with_check) from pg_policies ...
-- to see all four qualifiers. scripts/demo_m1.sh prints both columns.
--
-- Idempotent: guarded role creation, DROP POLICY IF EXISTS before each CREATE.

-- ---------------------------------------------------------------------------
-- 1. the application role
-- ---------------------------------------------------------------------------
DO $rls$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${APP_DB_USER}') THEN
        EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS',
                       '${APP_DB_USER}', '${APP_DB_PASSWORD}');
    ELSE
        EXECUTE format('ALTER ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS',
                       '${APP_DB_USER}', '${APP_DB_PASSWORD}');
    END IF;
END
$rls$;

GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO "${APP_DB_USER}";
GRANT USAGE ON SCHEMA public TO "${APP_DB_USER}";
GRANT SELECT, INSERT, UPDATE, DELETE ON memories TO "${APP_DB_USER}";

-- ---------------------------------------------------------------------------
-- 2. enable + force RLS
-- ---------------------------------------------------------------------------
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories FORCE  ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- 3. one policy per command, each scoped on BOTH subject_id and actor_id
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS memories_select_own ON memories;
CREATE POLICY memories_select_own ON memories
    FOR SELECT
    USING (
        subject_id = nullif(current_setting('app.subject_id', true), '')::uuid
        AND actor_id = nullif(current_setting('app.actor_id', true), '')::uuid
    );

DROP POLICY IF EXISTS memories_insert_own ON memories;
CREATE POLICY memories_insert_own ON memories
    FOR INSERT
    WITH CHECK (
        subject_id = nullif(current_setting('app.subject_id', true), '')::uuid
        AND actor_id = nullif(current_setting('app.actor_id', true), '')::uuid
    );

DROP POLICY IF EXISTS memories_update_own ON memories;
CREATE POLICY memories_update_own ON memories
    FOR UPDATE
    USING (
        subject_id = nullif(current_setting('app.subject_id', true), '')::uuid
        AND actor_id = nullif(current_setting('app.actor_id', true), '')::uuid
    )
    WITH CHECK (
        subject_id = nullif(current_setting('app.subject_id', true), '')::uuid
        AND actor_id = nullif(current_setting('app.actor_id', true), '')::uuid
    );

DROP POLICY IF EXISTS memories_delete_own ON memories;
CREATE POLICY memories_delete_own ON memories
    FOR DELETE
    USING (
        subject_id = nullif(current_setting('app.subject_id', true), '')::uuid
        AND actor_id = nullif(current_setting('app.actor_id', true), '')::uuid
    );
