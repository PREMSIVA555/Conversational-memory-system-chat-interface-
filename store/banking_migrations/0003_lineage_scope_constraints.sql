-- Additive T02.1 review hardening. 0002's recorded checksum stays immutable.
ALTER TABLE banking.preference_events ADD COLUMN source_span text;
ALTER TABLE banking.accepted_user_events ADD CONSTRAINT accepted_conversation_owner
 UNIQUE(id,customer_id,conversation_id);
ALTER TABLE banking.preference_events ADD CONSTRAINT preference_source_conversation
 FOREIGN KEY(source_user_event_id,customer_id,conversation_id)
 REFERENCES banking.accepted_user_events(id,customer_id,conversation_id);
ALTER TABLE banking.preferences_current ADD CONSTRAINT projection_value_consistency CHECK (
 (tombstoned AND value IS NULL AND key IN ('preferred_response_language','preferred_response_format')) OR
 (NOT tombstoned AND value IS NOT NULL AND
   ((key='preferred_response_language' AND value IN ('"en"'::jsonb,'"hi"'::jsonb)) OR
    (key='preferred_response_format' AND value IN ('"concise"'::jsonb,'"detailed"'::jsonb)))));
CREATE TABLE banking.memory_derivatives (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), customer_id uuid NOT NULL REFERENCES banking.customers,
 kind text NOT NULL CHECK(kind IN ('memory','summary','vector','cache','checkpoint')),
 UNIQUE(id,customer_id), UNIQUE(id,customer_id,kind)
);
ALTER TABLE banking.memory_derivatives ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.memory_derivatives FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_context ON banking.memory_derivatives
 USING(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid)
 WITH CHECK(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid);
ALTER TABLE banking.memory_lineage ADD CONSTRAINT lineage_derivative_owner
 FOREIGN KEY(derivative_id,customer_id,derivative_kind) REFERENCES banking.memory_derivatives(id,customer_id,kind);
