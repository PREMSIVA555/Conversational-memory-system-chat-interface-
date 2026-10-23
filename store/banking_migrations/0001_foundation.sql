-- P01 ownership roots only. Durable turns/preferences/outbox belong to P02.
CREATE TABLE IF NOT EXISTS banking.customers (
    customer_id uuid PRIMARY KEY,
    acceptance_sequence bigint NOT NULL DEFAULT 0 CHECK (acceptance_sequence >= 0)
);
CREATE TABLE IF NOT EXISTS banking.identity_links (
    issuer text NOT NULL,
    subject text NOT NULL,
    customer_id uuid NOT NULL REFERENCES banking.customers(customer_id),
    PRIMARY KEY (issuer, subject)
);
CREATE TABLE IF NOT EXISTS banking.conversations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id uuid NOT NULL REFERENCES banking.customers(customer_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, customer_id)
);
CREATE TABLE IF NOT EXISTS banking.conversation_scopes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL,
    customer_id uuid NOT NULL,
    FOREIGN KEY (conversation_id, customer_id)
        REFERENCES banking.conversations(id, customer_id)
);
CREATE TABLE IF NOT EXISTS banking.audit_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id uuid NOT NULL REFERENCES banking.customers(customer_id),
    conversation_id uuid NOT NULL,
    event text NOT NULL CHECK (event = 'conversation.created'),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (conversation_id, customer_id) REFERENCES banking.conversations(id, customer_id)
);
ALTER TABLE banking.audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.audit_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS audit_context ON banking.audit_events;
CREATE POLICY audit_context ON banking.audit_events
 USING (customer_id = nullif(current_setting('bank.customer_id',true),'')::uuid)
 WITH CHECK (customer_id = nullif(current_setting('bank.customer_id',true),'')::uuid);
ALTER TABLE banking.customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.customers FORCE ROW LEVEL SECURITY;
ALTER TABLE banking.conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.conversations FORCE ROW LEVEL SECURITY;
ALTER TABLE banking.conversation_scopes ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.conversation_scopes FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS customer_context ON banking.customers;
CREATE POLICY customer_context ON banking.customers
 USING (customer_id = nullif(current_setting('bank.customer_id',true),'')::uuid)
 WITH CHECK (customer_id = nullif(current_setting('bank.customer_id',true),'')::uuid);
DROP POLICY IF EXISTS conversation_context ON banking.conversations;
CREATE POLICY conversation_context ON banking.conversations
 USING (customer_id = nullif(current_setting('bank.customer_id',true),'')::uuid)
 WITH CHECK (customer_id = nullif(current_setting('bank.customer_id',true),'')::uuid);
DROP POLICY IF EXISTS scope_context ON banking.conversation_scopes;
CREATE POLICY scope_context ON banking.conversation_scopes
 USING (customer_id = nullif(current_setting('bank.customer_id',true),'')::uuid)
 WITH CHECK (customer_id = nullif(current_setting('bank.customer_id',true),'')::uuid);
-- Role names are quoted by store.banking_migrate, never interpolated unchecked.
GRANT USAGE ON SCHEMA banking TO @RUNTIME@, @AUTH@, @WORKER@;
GRANT SELECT ON banking.identity_links TO @AUTH@;
GRANT SELECT ON banking.customers TO @RUNTIME@, @WORKER@;
GRANT SELECT, INSERT ON banking.conversations, banking.conversation_scopes TO @RUNTIME@;
GRANT SELECT, INSERT ON banking.audit_events TO @RUNTIME@;
GRANT SELECT ON banking.conversations, banking.conversation_scopes TO @WORKER@;
-- Dispatcher has no customer-data grants until its P02 outbox exists.
CREATE OR REPLACE FUNCTION banking.keep_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Audit is append-only' USING ERRCODE='42501';
END; $$;
DROP TRIGGER IF EXISTS keep_audit ON banking.audit_events;
CREATE TRIGGER keep_audit BEFORE UPDATE OR DELETE ON banking.audit_events
 FOR EACH ROW EXECUTE FUNCTION banking.keep_audit();
