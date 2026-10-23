-- Additive source-validation hardening; recorded 0007 is immutable.
CREATE OR REPLACE FUNCTION banking.complete_preference(p_event uuid,p_owner uuid,p_generation bigint,p_proposals jsonb)
 RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE o banking.outbox_events; s banking.accepted_user_events; obtained uuid; item jsonb; outcome text:='persisted'; result text;
BEGIN
 o:=banking.lock_consumer(p_event,p_owner,p_generation);
 SELECT * INTO STRICT s FROM banking.accepted_user_events WHERE id=o.source_ref AND customer_id=o.customer_id;
 IF s.payload->>'handler'<>'preferences.v1' OR NOT EXISTS(SELECT 1 FROM banking.preference_proposals WHERE source_id=s.id)
 OR jsonb_typeof(p_proposals)<>'array' OR jsonb_array_length(p_proposals)>2 THEN
 RAISE EXCEPTION 'Invalid preference handler' USING ERRCODE='22023'; END IF;
 IF p_proposals IS DISTINCT FROM (SELECT proposals FROM banking.preference_proposals WHERE source_id=s.id) THEN
 RAISE EXCEPTION 'Proposal source mismatch' USING ERRCODE='22023'; END IF;
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

-- Existing acceptance.record must never consume a typed preference as a generic effect.
ALTER FUNCTION banking.complete_consumer(uuid,uuid,bigint,jsonb) RENAME TO complete_consumer_legacy;
REVOKE ALL ON FUNCTION banking.complete_consumer_legacy(uuid,uuid,bigint,jsonb) FROM @WORKER@;
CREATE FUNCTION banking.complete_consumer(p_event uuid,p_owner uuid,p_generation bigint,p_result jsonb)
 RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM banking.outbox_events o JOIN banking.accepted_user_events s ON s.id=o.source_ref
 WHERE o.id=p_event AND s.payload->>'handler'='preferences.v1') THEN
 RAISE EXCEPTION 'Preference handler required' USING ERRCODE='42501'; END IF;
 RETURN banking.complete_consumer_legacy(p_event,p_owner,p_generation,p_result);
END $$;
REVOKE ALL ON FUNCTION banking.complete_consumer(uuid,uuid,bigint,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.complete_consumer(uuid,uuid,bigint,jsonb) TO @WORKER@;

ALTER FUNCTION banking.accept_preference(uuid,text,uuid,jsonb,text,jsonb) RENAME TO accept_preference_internal;
REVOKE ALL ON FUNCTION banking.accept_preference_internal(uuid,text,uuid,jsonb,text,jsonb) FROM @RUNTIME@;
CREATE FUNCTION banking.accept_preference(p_client uuid,p_type text,p_conversation uuid,p_payload jsonb,p_session text,p_proposals jsonb)
 RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE item jsonb;
BEGIN
 IF p_payload->>'handler' IS DISTINCT FROM 'preferences.v1' OR jsonb_typeof(p_proposals) IS DISTINCT FROM 'array'
 OR jsonb_array_length(p_proposals)>2 THEN RAISE EXCEPTION 'Invalid preference source' USING ERRCODE='22023'; END IF;
 FOR item IN SELECT * FROM jsonb_array_elements(p_proposals) LOOP
  IF NOT (item ?& ARRAY['key','value','operation','scope_key']) OR item->>'operation' NOT IN ('SET','DELETE') OR
   item->>'key' NOT IN ('preferred_response_language','preferred_response_format') OR
   item->>'scope_key' NOT IN ('global','conversation:'||coalesce(p_conversation::text,''),'response:'||coalesce(p_conversation::text,'')) OR
   (item->>'scope_key'<>'global' AND p_conversation IS NULL) OR
   (item->>'operation'='SET' AND NOT ((item->>'key'='preferred_response_language' AND item->>'value' IN ('en','hi')) OR
    (item->>'key'='preferred_response_format' AND item->>'value' IN ('concise','detailed')))) OR
   (item->>'operation'='DELETE' AND item->'value'<>'null'::jsonb) THEN
   RAISE EXCEPTION 'Invalid typed proposal' USING ERRCODE='22023'; END IF;
 END LOOP;
 IF p_type='panel' AND (jsonb_array_length(p_proposals)<>1 OR
   p_proposals->0 IS DISTINCT FROM jsonb_build_object('key',p_payload->'key','value',p_payload->'value','operation',p_payload->'operation','scope_key',p_payload->'scope_key') OR
   p_proposals->0->>'scope_key' LIKE 'response:%') THEN
 RAISE EXCEPTION 'Invalid manual proposal' USING ERRCODE='22023'; END IF;
 RETURN banking.accept_preference_internal(p_client,p_type,p_conversation,p_payload,p_session,p_proposals);
END $$;
REVOKE ALL ON FUNCTION banking.accept_preference(uuid,text,uuid,jsonb,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.accept_preference(uuid,text,uuid,jsonb,text,jsonb) TO @RUNTIME@;
