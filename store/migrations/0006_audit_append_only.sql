-- 0006_audit_append_only.sql — M7 step 2: make `audit_log` append-only in the
-- DATABASE, not by convention.
--
-- M1 (0005_audit_feedback.sql) created the table and granted the application
-- role the full DML set, plus UPDATE/DELETE RLS policies. That is fine as a
-- shell, but an audit trail an application can rewrite is not an audit trail.
-- This migration removes the ability, at two independent levels:
--
--   1. PRIVILEGE  — REVOKE UPDATE, DELETE on audit_log from ${APP_DB_USER}.
--                   The privilege check runs *before* RLS and before any row is
--                   touched, so the app role gets a hard
--                   `42501 permission denied for table audit_log` on the first
--                   statement, whatever the WHERE clause says.
--
--   2. TRIGGER    — a BEFORE UPDATE OR DELETE row trigger that raises 42501 for
--                   any role that is not the table owner. This is the net that
--                   survives someone re-granting DML in a later migration (0005
--                   re-grants it on every `python -m store.migrate` run — this
--                   file runs after it and takes it back).
--
-- Belt and braces on purpose: level 1 alone would silently evaporate the moment
-- a future migration adds `GRANT ALL ON ALL TABLES`, and level 2 alone would be
-- bypassed by anything connecting as the owner.
--
-- WHY THE OWNER IS EXEMPT ----------------------------------------------------
-- The owner is the migration/maintenance role (DATABASE_URL), never the serving
-- path — `store/db.py` connects the application pool as the non-superuser
-- ${APP_DB_USER}. Retention pruning, test-fixture cleanup and schema surgery
-- have to remain possible for *someone*, and confining that to the role that
-- already owns the schema is the smallest workable exemption. The application,
-- the API and every request-path connection are on the other side of it.
--
-- WHY THE `ON DELETE SET NULL` CARVE-OUT -------------------------------------
-- `audit_log.memory_id REFERENCES memories(id) ON DELETE SET NULL` (0005) means
-- a hard DELETE of a memory row makes PostgreSQL issue an internal UPDATE
-- against audit_log. That is referential bookkeeping, not a rewrite of the
-- trail, so it is allowed — but only in its exact shape: memory_id going
-- non-NULL -> NULL with every other column byte-identical. Anything else is
-- still rejected. Without this carve-out, `DELETE FROM memories ...` (which the
-- integration suite's per-subject purge does) would fail with the append-only
-- error under any role that is not the owner.
--
-- Idempotent: CREATE OR REPLACE FUNCTION, DROP TRIGGER IF EXISTS before CREATE,
-- and REVOKE is a no-op when the privilege is already gone.

-- ---------------------------------------------------------------------------
-- 1. privilege level
-- ---------------------------------------------------------------------------
REVOKE UPDATE, DELETE ON audit_log FROM "${APP_DB_USER}";

-- 0005's `audit_log_update_own` / `audit_log_delete_own` RLS policies are
-- deliberately LEFT IN PLACE. Dropping them looks tidier and is actively worse:
-- with no policy, RLS makes the table non-updatable for the app role, so a
-- future `GRANT UPDATE` would produce a silent **0 rows affected** instead of an
-- error — and the trigger below would never see the row. Keeping the policies
-- means a re-grant lets the row reach the trigger, which raises loudly. This was
-- measured, not assumed: with the policies dropped, a re-granted UPDATE was
-- accepted silently and the trigger never fired.

-- ---------------------------------------------------------------------------
-- 2. trigger level
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    table_owner name;
BEGIN
    SELECT pg_get_userbyid(c.relowner)
      INTO table_owner
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relname = 'audit_log'
       AND n.nspname = 'public';

    -- Referential SET NULL from `memories` — allowed in this exact shape only.
    IF TG_OP = 'UPDATE'
       AND OLD.memory_id IS NOT NULL
       AND NEW.memory_id IS NULL
       AND NEW.id         IS NOT DISTINCT FROM OLD.id
       AND NEW.subject_id IS NOT DISTINCT FROM OLD.subject_id
       AND NEW.actor_id   IS NOT DISTINCT FROM OLD.actor_id
       AND NEW.action     IS NOT DISTINCT FROM OLD.action
       AND NEW.metadata   IS NOT DISTINCT FROM OLD.metadata
       AND NEW.created_at IS NOT DISTINCT FROM OLD.created_at
    THEN
        RETURN NEW;
    END IF;

    IF current_user IS DISTINCT FROM table_owner THEN
        RAISE EXCEPTION
            'audit_log is append-only: % is not permitted (role %)',
            TG_OP, current_user
            USING ERRCODE = '42501';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$fn$;

DROP TRIGGER IF EXISTS audit_log_append_only_trg ON audit_log;
CREATE TRIGGER audit_log_append_only_trg
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW
    EXECUTE FUNCTION audit_log_append_only();

COMMENT ON FUNCTION audit_log_append_only() IS
    'M7 step 2: rejects UPDATE/DELETE on audit_log for every role but the table owner.';
