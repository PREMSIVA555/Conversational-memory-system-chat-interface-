CREATE OR REPLACE FUNCTION banking.accept_turn(p_client uuid,p_conversation uuid,p_request jsonb,p_fingerprint text,p_ref text,p_binding text,p_seconds double precision)
RETURNS banking.turns LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE r banking.turns;s banking.accepted_user_events;rid uuid:=gen_random_uuid();oid uuid;customer uuid:=nullif(current_setting('bank.customer_id',true),'')::uuid;
BEGIN
 PERFORM 1 FROM banking.customers WHERE customer_id=customer FOR UPDATE;
 IF NOT FOUND OR length(p_binding)<>64 OR length(p_fingerprint)<>64 OR p_seconds NOT BETWEEN 0.01 AND 30 THEN RAISE EXCEPTION 'invalid turn' USING ERRCODE='42501';END IF;
 IF NOT EXISTS(SELECT 1 FROM banking.conversations WHERE id=p_conversation AND customer_id=customer) THEN RAISE EXCEPTION 'missing conversation' USING ERRCODE='42501';END IF;
 SELECT * INTO r FROM banking.turns WHERE customer_id=customer AND client_turn_id=p_client;
 IF FOUND THEN
  IF r.fingerprint<>p_fingerprint OR r.conversation_id<>p_conversation OR r.session_binding<>p_binding THEN RAISE EXCEPTION 'turn conflict' USING ERRCODE='23505';END IF;
  RETURN r;
 END IF;
 s:=banking.accept_user_event(p_client,'chat',p_conversation,p_request||jsonb_build_object('handler','turn.v1'));
 UPDATE banking.outbox_events SET event_type='turn.accepted' WHERE source_ref=s.id RETURNING id INTO oid;
 INSERT INTO banking.account_runs(id,customer_id,session_binding,fingerprint,deadline)
 VALUES(rid,customer,p_binding,p_request->>'run_fingerprint',clock_timestamp()+p_seconds*interval '1 second');
 INSERT INTO banking.turns(customer_id,conversation_id,client_turn_id,source_id,run_id,outbox_id,fingerprint,request,session_ref,session_binding)
 VALUES(customer,p_conversation,p_client,s.id,rid,oid,p_fingerprint,p_request-'text',p_ref,p_binding) RETURNING * INTO r;
 INSERT INTO banking.turn_events VALUES(r.id,customer,1,'turn.accepted',jsonb_build_object('status','accepted'),clock_timestamp());
 RETURN r;
END $$;

