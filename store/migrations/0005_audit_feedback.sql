-- 0005_audit_feedback.sql — governance tables.
--
-- audit_log : append-only trail of every write/read/delete/update/export.
--             M7 adds the database-level append-only enforcement
--             (0006_audit_append_only.sql); M1 only creates the table and its
--             RLS shell.
-- feedback  : per-memory user signal (thumbs up/down + free-text comment).
--
-- RLS is enabled and FORCED on both, with the same both-columns predicate used
-- on `memories`. `feedback` has no actor_id column in the plan's schema, so its
-- policies scope on subject_id and on the actor GUC being present — the
-- qualifier still names both settings, and the subject scoping is what carries
-- the auth boundary.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, DROP POLICY IF EXISTS before CREATE.

CREATE TABLE IF NOT EXISTS audit_log (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_id  uuid        NOT NULL,
    actor_id    uuid        NOT NULL,
    memory_id   uuid        NULL REFERENCES memories(id) ON DELETE SET NULL,
    action      text        NOT NULL,
    metadata    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_log_subject_created_idx
    ON audit_log (subject_id, created_at DESC);

CREATE TABLE IF NOT EXISTS feedback (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_id  uuid        NOT NULL,
    memory_id   uuid        NULL REFERENCES memories(id) ON DELETE SET NULL,
    signal      text        NOT NULL,
    comment     text        NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS feedback_subject_created_idx
    ON feedback (subject_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- grants for the non-superuser application role
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON audit_log TO "${APP_DB_USER}";
GRANT SELECT, INSERT, UPDATE, DELETE ON feedback  TO "${APP_DB_USER}";

-- ---------------------------------------------------------------------------
-- RLS on audit_log — scoped on both subject_id and actor_id
-- ---------------------------------------------------------------------------
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS audit_log_select_own ON audit_log;
CREATE POLICY audit_log_select_own ON audit_log
    FOR SELECT
    USING (
        subject_id = nullif(current_setting('app.subject_id', true), '')::uuid
        AND actor_id = nullif(current_setting('app.actor_id', true), '')::uuid
    );

DROP POLICY IF EXISTS audit_log_insert_own ON audit_log;
CREATE POLICY audit_log_insert_own ON audit_log
    FOR INSERT
    WITH CHECK (
        subject_id = nullif(current_setting('app.subject_id', true), '')::uuid
        AND actor_id = nullif(current_setting('app.actor_id', true), '')::uuid
    );

DROP POLICY IF EXISTS audit_log_update_own ON audit_log;
CREATE POLICY audit_log_update_own ON audit_log
    FOR UPDATE
    USING (
        subject_id = nullif(current_setting('app.subject_id', true), '')::uuid
        AND actor_id = nullif(current_setting('app.actor_id', true), '')::uuid
    )
    WITH CHECK (
        subject_id = nullif(current_setting('app.subject_id', true), '')::uuid
        AND actor_id = nullif(current_setting('app.actor_id', true), '')::uuid
    );

DROP POLICY IF EXISTS audit_log_delete_own ON audit_log;
CREATE POLICY audit_log_delete_own ON audit_log
    FOR DELETE
    USING (
        subject_id = nullif(current_setting('app.subject_id', true), '')::uuid
        AND actor_id = nullif(current_setting('app.actor_id', true), '')::uuid
    );

-- ---------------------------------------------------------------------------
-- RLS on feedback
-- ---------------------------------------------------------------------------
ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS feedback_select_own ON feedback;
CREATE POLICY feedback_select_own ON feedback
    FOR SELECT
    USING (
        subject_id = nullif(current_setting('app.subject_id', true), '')::uuid
        AND nullif(current_setting('app.actor_id', true), '') IS NOT NULL
    );

DROP POLICY IF EXISTS feedback_insert_own ON feedback;
CREATE POLICY feedback_insert_own ON feedback
    FOR INSERT
    WITH CHECK (
        subject_id = nullif(current_setting('app.subject_id', true), '')::uuid
        AND nullif(current_setting('app.actor_id', true), '') IS NOT NULL
    );

DROP POLICY IF EXISTS feedback_update_own ON feedback;
CREATE POLICY feedback_update_own ON feedback
    FOR UPDATE
    USING (
        subject_id = nullif(current_setting('app.subject_id', true), '')::uuid
        AND nullif(current_setting('app.actor_id', true), '') IS NOT NULL
    )
    WITH CHECK (
        subject_id = nullif(current_setting('app.subject_id', true), '')::uuid
        AND nullif(current_setting('app.actor_id', true), '') IS NOT NULL
    );

DROP POLICY IF EXISTS feedback_delete_own ON feedback;
CREATE POLICY feedback_delete_own ON feedback
    FOR DELETE
    USING (
        subject_id = nullif(current_setting('app.subject_id', true), '')::uuid
        AND nullif(current_setting('app.actor_id', true), '') IS NOT NULL
    );
