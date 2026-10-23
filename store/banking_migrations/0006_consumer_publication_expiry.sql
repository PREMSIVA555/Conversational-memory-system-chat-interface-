-- Validate lease after potentially waiting terminal writes; expired transactions roll back.
CREATE OR REPLACE FUNCTION banking.complete_consumer(p_event uuid,p_owner uuid,p_generation bigint,p_result jsonb)
 RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE o banking.outbox_events; obtained uuid; expected_result jsonb;
BEGIN
 -- Completed duplicate may observe only its own committed operation, with original ownership.
 SELECT * INTO o FROM banking.outbox_events WHERE id=p_event AND
 customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid FOR UPDATE;
 IF FOUND AND o.status='completed' AND o.locked_by=p_owner::text AND o.lease_generation=p_generation THEN
  RETURN 'duplicate'; END IF;
 o:=banking.lock_consumer(p_event,p_owner,p_generation);
 IF o.event_type<>'user.accepted' OR o.schema_version<>1 OR o.attempt>3 THEN
  RAISE EXCEPTION 'Unsupported or exhausted work' USING ERRCODE='22023'; END IF;
 SELECT jsonb_build_object('handler','acceptance.record','version',1,'source_sequence',s.sequence)
 INTO expected_result FROM banking.accepted_user_events s WHERE s.id=o.source_ref AND s.customer_id=o.customer_id;
 IF p_result IS NOT NULL AND p_result IS DISTINCT FROM expected_result THEN
  RAISE EXCEPTION 'Invalid local result' USING ERRCODE='22023'; END IF;
 -- INV21: obtain the unique receipt BEFORE any local effect, in this same transaction.
 INSERT INTO banking.handler_receipts(event_id,customer_id,handler_name,handler_version,outcome)
 VALUES(o.id,o.customer_id,'acceptance.record','1','committed')
 ON CONFLICT(event_id,handler_name,handler_version) DO NOTHING RETURNING event_id INTO obtained;
 IF obtained IS NOT NULL THEN
  INSERT INTO banking.local_operations(event_id,customer_id,result) VALUES(o.id,o.customer_id,expected_result);
 END IF;
 PERFORM banking.lock_consumer(p_event,p_owner,p_generation);
 INSERT INTO banking.consumer_terminal_events(event_id,customer_id,generation,status)
 VALUES(o.id,o.customer_id,p_generation,'completed');
 PERFORM banking.lock_consumer(p_event,p_owner,p_generation);
 UPDATE banking.outbox_events SET status='completed',completed_at=clock_timestamp(),failure_code=NULL WHERE id=o.id;
 RETURN CASE WHEN obtained IS NULL THEN 'duplicate' ELSE 'applied' END;
END $$;
CREATE OR REPLACE FUNCTION banking.fail_consumer(p_event uuid,p_owner uuid,p_generation bigint,p_code text)
 RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE o banking.outbox_events; next_status text;
BEGIN
 o:=banking.lock_consumer(p_event,p_owner,p_generation);
 IF p_code NOT IN ('transient_unavailable','transient_timeout','unsupported_schema','unsupported_type','permanent','retry_exhausted') OR p_code IS NULL THEN
  RAISE EXCEPTION 'Unclassified failure' USING ERRCODE='22023'; END IF;
 next_status:=CASE WHEN p_code IN ('transient_unavailable','transient_timeout') AND o.attempt<3
  THEN 'pending' ELSE 'dead_letter' END;
 UPDATE banking.outbox_events SET status=next_status,failure_code=p_code,
 available_at=clock_timestamp()+make_interval(secs=>least(30,cast(power(2,o.attempt) AS int))),
 locked_until=NULL,completed_at=CASE WHEN next_status='dead_letter' THEN clock_timestamp() END WHERE id=o.id;
 IF next_status='dead_letter' THEN
  INSERT INTO banking.consumer_terminal_events(event_id,customer_id,generation,status)
  VALUES(o.id,o.customer_id,p_generation,'dead_letter');
 END IF;
 IF o.locked_until<=clock_timestamp() THEN
  RAISE EXCEPTION 'Expired consumer publication' USING ERRCODE='42501'; END IF;
 RETURN next_status;
END $$;
