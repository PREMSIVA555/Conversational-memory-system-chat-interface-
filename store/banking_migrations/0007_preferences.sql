-- T02.3 typed proposals, exact reads and receipt-gated publication. No legacy cutover.
CREATE TABLE banking.preference_proposals (
 source_id uuid PRIMARY KEY, customer_id uuid NOT NULL, session_binding text NOT NULL,
 status text NOT NULL CHECK(status IN ('pending','persisted','ignored_stale','failed','temporary')),
 proposals jsonb NOT NULL, failure_code text,
 FOREIGN KEY(source_id,customer_id) REFERENCES banking.accepted_user_events(id,customer_id)
);
ALTER TABLE banking.preference_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.preference_proposals FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_context ON banking.preference_proposals USING(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid)
 WITH CHECK(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid);
GRANT SELECT ON banking.preference_proposals TO @RUNTIME@,@WORKER@;
CREATE FUNCTION banking.accept_preference(p_client uuid,p_type text,p_conversation uuid,p_payload jsonb,p_session text,p_proposals jsonb)
 RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE s banking.accepted_user_events; customer uuid:=nullif(current_setting('bank.customer_id',true),'')::uuid; n bigint; v bigint; lease record;
BEGIN
 IF length(p_session)<>64 OR p_payload->>'handler'<>'preferences.v1' OR p_type NOT IN ('panel','chat') OR jsonb_typeof(p_proposals)<>'array' THEN
 RAISE EXCEPTION 'Invalid preference envelope' USING ERRCODE='22023'; END IF;
 PERFORM 1 FROM banking.customers WHERE customer_id=customer FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Missing context' USING ERRCODE='42501'; END IF;
 SELECT * INTO s FROM banking.accepted_user_events WHERE customer_id=customer AND client_event_id=p_client;
 IF FOUND THEN
  IF s.payload IS DISTINCT FROM p_payload OR s.event_type IS DISTINCT FROM p_type OR s.conversation_id IS DISTINCT FROM p_conversation THEN
   RAISE EXCEPTION 'Idempotency conflict' USING ERRCODE='23505'; END IF;
 ELSE
  IF p_type='panel' THEN
   SELECT version INTO v FROM banking.preferences_current WHERE customer_id=customer AND key=p_payload->>'key' AND scope_key=p_payload->>'scope_key';
   IF coalesce(v,0) IS DISTINCT FROM (p_payload->>'expected_version')::bigint THEN RAISE EXCEPTION 'Stale panel version' USING ERRCODE='40001'; END IF;
  END IF;
  UPDATE banking.customers SET acceptance_sequence=acceptance_sequence+1 WHERE customer_id=customer RETURNING acceptance_sequence INTO n;
  INSERT INTO banking.accepted_user_events(customer_id,actor_ref,sequence,event_type,conversation_id,client_event_id,payload)
   VALUES(customer,'user',n,p_type,p_conversation,p_client,p_payload) RETURNING * INTO s;
  INSERT INTO banking.outbox_events(customer_id,source_ref) VALUES(customer,s.id);
 END IF;
 IF EXISTS(SELECT 1 FROM banking.preference_proposals WHERE source_id=s.id AND session_binding<>p_session) THEN
 RAISE EXCEPTION 'Session conflict' USING ERRCODE='23505'; END IF;
 INSERT INTO banking.preference_proposals(source_id,customer_id,session_binding,status,proposals)
 VALUES(s.id,s.customer_id,p_session,'pending',p_proposals)
 ON CONFLICT(source_id) DO NOTHING;
 IF p_type='panel' AND EXISTS(SELECT 1 FROM banking.preference_proposals WHERE source_id=s.id AND status='pending') THEN
  SELECT * INTO lease FROM banking.claim_outbox(gen_random_uuid(),1,15,(SELECT id FROM banking.outbox_events WHERE source_ref=s.id));
  PERFORM banking.complete_preference(lease.event_id,lease.owner,lease.generation,p_proposals);
 END IF;
 RETURN s.id;
END $$;
REVOKE ALL ON FUNCTION banking.accept_preference(uuid,text,uuid,jsonb,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.accept_preference(uuid,text,uuid,jsonb,text,jsonb) TO @RUNTIME@;
CREATE FUNCTION banking.claim_preference(p_event uuid,p_owner uuid)
 RETURNS TABLE(event_id uuid,customer_id uuid,generation bigint,owner uuid)
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM banking.outbox_events o JOIN banking.preference_proposals p ON p.source_id=o.source_ref
 WHERE o.id=p_event AND o.customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid) THEN
 RAISE EXCEPTION 'Missing preference work' USING ERRCODE='42501'; END IF;
 RETURN QUERY SELECT * FROM banking.claim_outbox(p_owner,1,15,p_event);
END $$;
REVOKE ALL ON FUNCTION banking.claim_preference(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.claim_preference(uuid,uuid) TO @WORKER@;
CREATE FUNCTION banking.complete_preference(p_event uuid,p_owner uuid,p_generation bigint,p_proposals jsonb)
 RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE o banking.outbox_events; s banking.accepted_user_events; obtained uuid; item jsonb; outcome text:='persisted'; result text;
BEGIN
 o:=banking.lock_consumer(p_event,p_owner,p_generation);
 SELECT * INTO STRICT s FROM banking.accepted_user_events WHERE id=o.source_ref AND customer_id=o.customer_id;
 IF s.payload->>'handler'<>'preferences.v1' OR NOT EXISTS(SELECT 1 FROM banking.preference_proposals WHERE source_id=s.id)
 OR jsonb_typeof(p_proposals)<>'array' OR jsonb_array_length(p_proposals)>2 THEN
 RAISE EXCEPTION 'Invalid preference handler' USING ERRCODE='22023'; END IF;
 INSERT INTO banking.handler_receipts(event_id,customer_id,handler_name,handler_version,outcome)
 VALUES(o.id,o.customer_id,'preferences.apply','1','committed') ON CONFLICT DO NOTHING RETURNING event_id INTO obtained;
 IF obtained IS NOT NULL THEN
  IF jsonb_array_length(p_proposals)=0 THEN outcome:='failed'; END IF;
  FOR item IN SELECT * FROM jsonb_array_elements(p_proposals) LOOP
   IF item->>'scope_key' LIKE 'response:%' AND item->>'scope_key'='response:'||s.conversation_id::text THEN
    outcome:='temporary';
   ELSE
    result:=banking.project_preference_internal(s.id,item->>'key',item->>'scope_key',
      CASE WHEN item->>'scope_key'='global' THEN NULL ELSE s.conversation_id END,item->>'operation',
      CASE WHEN item->>'operation'='DELETE' THEN NULL ELSE item->'value' END);
    IF result='ignored_stale' THEN outcome:='ignored_stale'; END IF;
   END IF;
  END LOOP;
  UPDATE banking.preference_proposals SET status=outcome,proposals=p_proposals,
   failure_code=CASE WHEN outcome='failed' THEN 'extraction_failed' END WHERE source_id=s.id;
 END IF;
 PERFORM banking.lock_consumer(p_event,p_owner,p_generation);
 INSERT INTO banking.consumer_terminal_events(event_id,customer_id,generation,status) VALUES(o.id,o.customer_id,p_generation,'completed');
 PERFORM banking.lock_consumer(p_event,p_owner,p_generation);
 UPDATE banking.outbox_events SET status='completed',completed_at=clock_timestamp(),failure_code=NULL WHERE id=o.id;
 RETURN CASE WHEN obtained IS NULL THEN 'duplicate' ELSE 'applied' END;
END $$;
REVOKE ALL ON FUNCTION banking.complete_preference(uuid,uuid,bigint,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.complete_preference(uuid,uuid,bigint,jsonb) TO @WORKER@;
ALTER FUNCTION banking.project_preference(uuid,text,text,uuid,text,jsonb) RENAME TO project_preference_internal;
REVOKE ALL ON FUNCTION banking.project_preference_internal(uuid,text,text,uuid,text,jsonb) FROM @WORKER@;
CREATE FUNCTION banking.project_preference(p_source uuid,p_key text,p_scope text,p_conversation uuid,p_operation text,p_value jsonb)
 RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM banking.accepted_user_events WHERE id=p_source AND payload->>'handler'='preferences.v1') THEN
 RAISE EXCEPTION 'Typed preference requires receipt gate' USING ERRCODE='42501'; END IF;
 RETURN banking.project_preference_internal(p_source,p_key,p_scope,p_conversation,p_operation,p_value);
END $$;
REVOKE ALL ON FUNCTION banking.project_preference(uuid,text,text,uuid,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.project_preference(uuid,text,text,uuid,text,jsonb) TO @WORKER@;
CREATE FUNCTION banking.preference_outbox(p_source uuid) RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
 SELECT o.id FROM banking.outbox_events o JOIN banking.preference_proposals p ON p.source_id=o.source_ref
 WHERE o.source_ref=p_source AND o.customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid
$$;
REVOKE ALL ON FUNCTION banking.preference_outbox(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.preference_outbox(uuid) TO @RUNTIME@;
