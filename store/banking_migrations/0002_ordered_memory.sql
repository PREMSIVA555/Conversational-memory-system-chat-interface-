-- T02.1 additive foundations only: no consumer, extraction, reader or cutover.
CREATE TABLE IF NOT EXISTS banking.accepted_user_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), customer_id uuid NOT NULL REFERENCES banking.customers,
 actor_ref text NOT NULL CHECK (actor_ref = 'user'), sequence bigint NOT NULL CHECK (sequence > 0),
 event_type text NOT NULL CHECK (event_type IN ('chat','panel')), conversation_id uuid,
 accepted_at timestamptz NOT NULL DEFAULT now(), client_event_id uuid NOT NULL, payload jsonb NOT NULL,
 UNIQUE(customer_id,sequence), UNIQUE(customer_id,client_event_id), UNIQUE(id,customer_id),
 FOREIGN KEY(conversation_id,customer_id) REFERENCES banking.conversations(id,customer_id)
);
CREATE TABLE IF NOT EXISTS banking.preference_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), customer_id uuid NOT NULL,
 source_user_event_id uuid NOT NULL, source_turn_id uuid, customer_sequence bigint NOT NULL,
 key text NOT NULL CHECK(key IN ('preferred_response_language','preferred_response_format')),
 scope_key text NOT NULL, conversation_id uuid, operation text NOT NULL CHECK(operation IN ('SET','DELETE')),
 normalized_value jsonb, status text NOT NULL CHECK(status IN ('persisted','ignored_stale')),
 CHECK ((scope_key='global' AND conversation_id IS NULL) OR (conversation_id IS NOT NULL AND scope_key='conversation:'||conversation_id::text)),
 CHECK ((operation='DELETE' AND normalized_value IS NULL) OR (operation='SET' AND normalized_value IS NOT NULL AND
   ((key='preferred_response_language' AND normalized_value IN ('"en"'::jsonb,'"hi"'::jsonb)) OR
    (key='preferred_response_format' AND normalized_value IN ('"concise"'::jsonb,'"detailed"'::jsonb))))),
 UNIQUE(source_user_event_id,key,scope_key), UNIQUE(id,customer_id),
 FOREIGN KEY(source_user_event_id,customer_id) REFERENCES banking.accepted_user_events(id,customer_id),
 FOREIGN KEY(conversation_id,customer_id) REFERENCES banking.conversations(id,customer_id)
);
CREATE TABLE IF NOT EXISTS banking.preferences_current (
 customer_id uuid NOT NULL, entity_key text NOT NULL DEFAULT 'self' CHECK(entity_key='self'),
 key text NOT NULL, scope_key text NOT NULL, value jsonb, tombstoned boolean NOT NULL,
 version bigint NOT NULL CHECK(version>0), last_sequence bigint NOT NULL CHECK(last_sequence>0), source_event_id uuid NOT NULL,
 PRIMARY KEY(customer_id,entity_key,key,scope_key),
 FOREIGN KEY(source_event_id,customer_id) REFERENCES banking.preference_events(id,customer_id)
);
CREATE TABLE IF NOT EXISTS banking.outbox_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), customer_id uuid NOT NULL, source_ref uuid NOT NULL,
 event_type text NOT NULL DEFAULT 'user.accepted', schema_version int NOT NULL DEFAULT 1 CHECK(schema_version=1),
 minimal_payload jsonb NOT NULL DEFAULT '{}', trace_context jsonb NOT NULL DEFAULT '{}',
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','claimed','completed','dead_letter')),
 attempt int NOT NULL DEFAULT 0 CHECK(attempt>=0), available_at timestamptz NOT NULL DEFAULT now(),
 locked_until timestamptz, locked_by text, lease_generation bigint NOT NULL DEFAULT 0 CHECK(lease_generation>=0),
 fencing_token bigint NOT NULL DEFAULT 0 CHECK(fencing_token>=0),
 UNIQUE(source_ref,event_type), UNIQUE(id,customer_id),
 FOREIGN KEY(source_ref,customer_id) REFERENCES banking.accepted_user_events(id,customer_id)
);
CREATE INDEX IF NOT EXISTS outbox_claimable ON banking.outbox_events(status,available_at,locked_until);
CREATE TABLE IF NOT EXISTS banking.handler_receipts (
 event_id uuid NOT NULL, customer_id uuid NOT NULL, handler_name text NOT NULL, handler_version text NOT NULL,
 outcome text NOT NULL, processed_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(event_id,handler_name,handler_version),
 FOREIGN KEY(event_id,customer_id) REFERENCES banking.outbox_events(id,customer_id)
);
CREATE TABLE IF NOT EXISTS banking.memory_lineage (
 derivative_id uuid NOT NULL, customer_id uuid NOT NULL, source_user_event_id uuid NOT NULL,
 derivative_kind text NOT NULL CHECK(derivative_kind IN ('memory','summary','vector','cache','checkpoint')),
 suppressed_at timestamptz, cleanup_status text NOT NULL DEFAULT 'retained' CHECK(cleanup_status IN ('retained','pending','deleted')),
 PRIMARY KEY(derivative_id,source_user_event_id),
 FOREIGN KEY(source_user_event_id,customer_id) REFERENCES banking.accepted_user_events(id,customer_id)
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['accepted_user_events','preference_events','preferences_current','outbox_events','handler_receipts','memory_lineage'] LOOP
  EXECUTE format('ALTER TABLE banking.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE banking.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('DROP POLICY IF EXISTS tenant_context ON banking.%I',t);
  EXECUTE format('CREATE POLICY tenant_context ON banking.%I USING (customer_id=nullif(current_setting(''bank.customer_id'',true),'''')::uuid) WITH CHECK (customer_id=nullif(current_setting(''bank.customer_id'',true),'''')::uuid)',t);
 END LOOP;
END $$;
GRANT SELECT ON banking.accepted_user_events,banking.preference_events,banking.preferences_current TO @RUNTIME@,@WORKER@;
-- No worker/dispatcher mutation grants until T02.2's receipt/fencing implementation.
CREATE OR REPLACE FUNCTION banking.accept_user_event(p_client uuid,p_type text,p_conversation uuid,p_payload jsonb)
 RETURNS banking.accepted_user_events LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE customer uuid := nullif(current_setting('bank.customer_id',true),'')::uuid;
 n bigint; current_version bigint; result banking.accepted_user_events;
BEGIN
 -- One existing customer row serializes duplicate acceptance and empty-key writers.
 PERFORM 1 FROM banking.customers WHERE customer_id=customer FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Missing customer context' USING ERRCODE='42501'; END IF;
 SELECT * INTO result FROM banking.accepted_user_events WHERE customer_id=customer AND client_event_id=p_client;
 IF FOUND THEN
  IF result.payload IS DISTINCT FROM p_payload OR result.event_type IS DISTINCT FROM p_type OR result.conversation_id IS DISTINCT FROM p_conversation THEN
   RAISE EXCEPTION 'Idempotency conflict' USING ERRCODE='23505';
  END IF;
  RETURN result;
 END IF;
 IF p_type='panel' THEN
  IF NOT (p_payload ?& ARRAY['key','scope_key','operation','expected_version']) OR
     jsonb_typeof(p_payload->'expected_version') <> 'number' THEN
   RAISE EXCEPTION 'Panel CAS required' USING ERRCODE='22023';
  END IF;
  SELECT version INTO current_version FROM banking.preferences_current WHERE customer_id=customer
   AND entity_key='self' AND key=p_payload->>'key' AND scope_key=p_payload->>'scope_key';
  IF coalesce(current_version,0) IS DISTINCT FROM (p_payload->>'expected_version')::bigint THEN
   RAISE EXCEPTION 'Stale panel version' USING ERRCODE='40001';
  END IF;
 END IF;
 UPDATE banking.customers SET acceptance_sequence=acceptance_sequence+1 WHERE customer_id=customer RETURNING acceptance_sequence INTO n;
 INSERT INTO banking.accepted_user_events(customer_id,actor_ref,sequence,event_type,conversation_id,client_event_id,payload)
 VALUES(customer,'user',n,p_type,p_conversation,p_client,p_payload) RETURNING * INTO result;
 INSERT INTO banking.outbox_events(customer_id,source_ref) VALUES(customer,result.id);
 IF p_type='panel' THEN
  PERFORM banking.project_preference(result.id,p_payload->>'key',p_payload->>'scope_key',
   CASE WHEN p_payload->>'scope_key'='global' THEN NULL ELSE p_conversation END,
   p_payload->>'operation',CASE WHEN p_payload->>'operation'='DELETE' THEN NULL ELSE p_payload->'value' END);
 END IF;
 RETURN result;
END $$;
CREATE OR REPLACE FUNCTION banking.project_preference(p_source uuid,p_key text,p_scope text,p_conversation uuid,p_operation text,p_value jsonb)
 RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE customer uuid := nullif(current_setting('bank.customer_id',true),'')::uuid;
 source banking.accepted_user_events; e banking.preference_events; changed int;
BEGIN
 PERFORM 1 FROM banking.customers WHERE customer_id=customer FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Missing customer context' USING ERRCODE='42501'; END IF;
 SELECT * INTO STRICT source FROM banking.accepted_user_events WHERE id=p_source AND customer_id=customer;
 SELECT * INTO e FROM banking.preference_events WHERE source_user_event_id=p_source AND key=p_key AND scope_key=p_scope;
 IF FOUND THEN
  IF e.operation IS DISTINCT FROM p_operation OR e.normalized_value IS DISTINCT FROM p_value OR e.conversation_id IS DISTINCT FROM p_conversation THEN
   RAISE EXCEPTION 'Preference event conflict' USING ERRCODE='23505';
  END IF;
  RETURN e.status;
 END IF;
 INSERT INTO banking.preference_events(customer_id,source_user_event_id,customer_sequence,key,scope_key,conversation_id,operation,normalized_value,status)
 VALUES(customer,p_source,source.sequence,p_key,p_scope,p_conversation,p_operation,p_value,'persisted') RETURNING * INTO e;
 INSERT INTO banking.preferences_current(customer_id,key,scope_key,value,tombstoned,version,last_sequence,source_event_id)
 VALUES(customer,p_key,p_scope,p_value,p_operation='DELETE',1,source.sequence,e.id)
 ON CONFLICT(customer_id,entity_key,key,scope_key) DO UPDATE SET value=EXCLUDED.value,tombstoned=EXCLUDED.tombstoned,
 version=banking.preferences_current.version+1,last_sequence=EXCLUDED.last_sequence,source_event_id=EXCLUDED.source_event_id
 WHERE banking.preferences_current.last_sequence<EXCLUDED.last_sequence;
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed=0 THEN UPDATE banking.preference_events SET status='ignored_stale' WHERE id=e.id; RETURN 'ignored_stale'; END IF;
 RETURN 'persisted';
END $$;
REVOKE ALL ON FUNCTION banking.accept_user_event(uuid,text,uuid,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION banking.project_preference(uuid,text,text,uuid,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.accept_user_event(uuid,text,uuid,jsonb) TO @RUNTIME@;
GRANT EXECUTE ON FUNCTION banking.project_preference(uuid,text,text,uuid,text,jsonb) TO @WORKER@;
