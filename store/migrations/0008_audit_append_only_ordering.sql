-- 0008_audit_append_only_ordering.sql — close a hole in 0006's trigger.
--
-- 0006 installed `audit_log_append_only()` with its checks in the wrong order:
--
--     1. if this UPDATE looks like a referential ON DELETE SET NULL -> ALLOW
--     2. if current_user is not the table owner                     -> RAISE
--
-- Step 1 asked only what the row change *looked like*, never who was making it.
-- So the carve-out was granted to every role, not just to referential
-- enforcement. In exactly the scenario layer 2 exists to survive — a future
-- `GRANT ALL ON ALL TABLES IN SCHEMA public` restoring UPDATE to the app role —
-- a non-owner could run
--
--     UPDATE audit_log SET memory_id = NULL;
--
-- and strip the memory linkage from the entire trail without an error. The
-- `action`, `subject_id` and timestamps survived, so it was not total erasure,
-- but an audit row that no longer says *which memory* it is about has lost most
-- of its value, and the trigger reported success.
--
-- Not exploitable as shipped: 0006's REVOKE (layer 1) denies UPDATE outright, so
-- the trigger is unreachable for the app role. The defect is that layer 2 did
-- not survive the re-GRANT it was built to survive — which is the only scenario
-- it has.
--
-- THE FIX: ask who first.
--
--     1. if current_user is not the table owner -> RAISE
--     2. otherwise -> allow
--
-- and the SET NULL carve-out is deleted rather than reordered, because it turns
-- out to be unnecessary. PostgreSQL runs referential integrity actions with the
-- privileges of the referenced table's owner, so the internal UPDATE that
-- `ON DELETE SET NULL` issues arrives at this trigger with `current_user`
-- already equal to the owner and passes the ownership check on its own. That is
-- verified, not assumed: with this migration applied, an app-role
-- `DELETE FROM memories` (which fires the SET NULL) still succeeds, while an
-- app-role `UPDATE audit_log SET memory_id = NULL` with UPDATE re-granted is
-- rejected. A shape-matching carve-out cannot tell those two apart; the role can.
--
-- 0006 is committed and is left untouched. This file supersedes its function
-- body via CREATE OR REPLACE; the trigger from 0006 keeps pointing at the same
-- function name and so picks the new body up with no trigger change needed.
--
-- Numbered 0008 rather than 0007: M8 owns 0007.
--
-- Idempotent: CREATE OR REPLACE FUNCTION.

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

    -- WHO, before WHAT. Every non-owner mutation is refused here, whatever the
    -- row change looks like — including one shaped exactly like a referential
    -- SET NULL. Referential actions reach this point as the owner and are
    -- unaffected.
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

COMMENT ON FUNCTION audit_log_append_only() IS
    'M7 steps 2 + D4: rejects UPDATE/DELETE on audit_log for every role but the '
    'table owner. Ownership is checked FIRST, so no row-shape carve-out can be '
    'used to bypass it.';
