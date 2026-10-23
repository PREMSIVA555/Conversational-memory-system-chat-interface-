-- T03.3 durable acceptance and transport; existing migrations remain immutable.
CREATE TABLE banking.turns (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),customer_id uuid NOT NULL,
 conversation_id uuid NOT NULL,client_turn_id uuid NOT NULL,source_id uuid NOT NULL,
 run_id uuid NOT NULL UNIQUE REFERENCES banking.account_runs(id),outbox_id uuid NOT NULL UNIQUE REFERENCES banking.outbox_events(id),
 fingerprint text NOT NULL,request jsonb NOT NULL,session_ref text NOT NULL,session_binding text NOT NULL,
 accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),status text NOT NULL DEFAULT 'accepted',
 result jsonb,generation bigint NOT NULL DEFAULT 0,lease_until timestamptz,attempts int NOT NULL DEFAULT 0,
 UNIQUE(customer_id,client_turn_id),FOREIGN KEY(conversation_id,customer_id) REFERENCES banking.conversations(id,customer_id),
 FOREIGN KEY(source_id,customer_id) REFERENCES banking.accepted_user_events(id,customer_id)
);
CREATE TABLE banking.turn_events (
 turn_id uuid NOT NULL REFERENCES banking.turns(id),customer_id uuid NOT NULL,event_id bigint NOT NULL,
 event_type text NOT NULL,payload jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(turn_id,event_id)
);
ALTER TABLE banking.turns ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.turns FORCE ROW LEVEL SECURITY;
ALTER TABLE banking.turn_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.turn_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant ON banking.turns USING(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid);
CREATE POLICY tenant ON banking.turn_events USING(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid);
CREATE POLICY turn_dispatch ON banking.turns TO @MIGRATOR@ USING(true) WITH CHECK(true);
CREATE POLICY turn_dispatch ON banking.turn_events TO @MIGRATOR@ USING(true) WITH CHECK(true);
GRANT SELECT ON banking.turns,banking.turn_events TO @RUNTIME@,@WORKER@;
CREATE FUNCTION banking.accept_turn(p_client uuid,p_conversation uuid,p_request jsonb,p_fingerprint text,p_ref text,p_binding text,p_seconds double precision)
RETURNS banking.turns LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE r banking.turns;s banking.accepted_user_events;rid uuid:=gen_random_uuid();oid uuid;customer uuid:=nullif(current_setting('bank.customer_id',true),'')::uuid;
BEGIN
 PERFORM 1 FROM banking.customers WHERE customer_id=customer FOR UPDATE;
 IF NOT FOUND OR length(p_binding)<>64 OR length(p_fingerprint)<>64 OR p_seconds NOT BETWEEN 0.01 AND 30 THEN RAISE EXCEPTION 'invalid turn' USING ERRCODE='42501';END IF;
 IF NOT EXISTS(SELECT 1 FROM banking.conversations WHERE id=p_conversation AND customer_id=customer) THEN RAISE EXCEPTION 'missing conversation' USING ERRCODE='42501';END IF;
 SELECT * INTO r FROM banking.turns WHERE customer_id=customer AND client_turn_id=p_client;
 IF FOUND THEN
  IF r.fingerprint<>p_fingerprint OR r.conversation_id<>p_conversation THEN RAISE EXCEPTION 'turn conflict' USING ERRCODE='23505';END IF;
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
CREATE FUNCTION banking.claim_turn() RETURNS banking.turns LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE r banking.turns;
BEGIN
 SELECT t.* INTO r FROM banking.turns t JOIN banking.outbox_events o ON o.id=t.outbox_id WHERE t.result IS NULL AND o.failure_code IS DISTINCT FROM 'source_deleted' AND (t.lease_until IS NULL OR t.lease_until<=clock_timestamp()) ORDER BY t.accepted_at,t.id FOR UPDATE OF t SKIP LOCKED LIMIT 1;
 IF r.id IS NULL THEN RETURN r;END IF;
 UPDATE banking.turns SET generation=generation+1,attempts=attempts+1,lease_until=clock_timestamp()+interval '35 seconds',status='running' WHERE id=r.id RETURNING * INTO r;
 INSERT INTO banking.turn_events SELECT r.id,r.customer_id,coalesce(max(event_id),0)+1,'turn.running',jsonb_build_object('status','running','attempt',r.attempts),clock_timestamp() FROM banking.turn_events WHERE turn_id=r.id;
 RETURN r;
END $$;
CREATE FUNCTION banking.finish_turn(p_id uuid,p_generation bigint,p_result jsonb) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE r banking.turns;kind text;
BEGIN
 SELECT * INTO r FROM banking.turns WHERE id=p_id FOR UPDATE;
 IF r.id IS NULL OR r.generation<>p_generation OR r.result IS NOT NULL OR r.lease_until<=clock_timestamp() THEN RETURN false;END IF;
 IF EXISTS(SELECT 1 FROM banking.erasure_ledger WHERE source_id=r.source_id) THEN RETURN false;END IF;
 IF p_result->>'status' NOT IN ('session_expired','expired','unavailable') AND NOT EXISTS(SELECT 1 FROM banking.account_runs WHERE id=r.run_id AND result=p_result) THEN RETURN false;END IF;
 UPDATE banking.turns SET result=p_result,status=p_result->>'status',lease_until=NULL WHERE id=p_id;
 kind:=CASE WHEN p_result->>'status' IN ('expired','unavailable','session_expired') THEN 'turn.failed' ELSE 'answer.ready' END;
 INSERT INTO banking.turn_events SELECT r.id,r.customer_id,coalesce(max(event_id),0)+1,kind,jsonb_build_object('status',p_result->>'status','result',p_result),clock_timestamp() FROM banking.turn_events WHERE turn_id=r.id;
 UPDATE banking.outbox_events SET status='completed',completed_at=clock_timestamp() WHERE id=r.outbox_id;
 RETURN true;
END $$;
REVOKE ALL ON FUNCTION banking.accept_turn(uuid,uuid,jsonb,text,text,text,double precision),banking.claim_turn(),banking.finish_turn(uuid,bigint,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.accept_turn(uuid,uuid,jsonb,text,text,text,double precision) TO @RUNTIME@;
GRANT EXECUTE ON FUNCTION banking.claim_turn(),banking.finish_turn(uuid,bigint,jsonb) TO @WORKER@;
-- Correct qualified RETURNING after dev-01 exposed PL/pgSQL OUT-name ambiguity.
CREATE OR REPLACE FUNCTION banking.claim_outbox(p_owner uuid,p_limit int DEFAULT 16,p_lease_seconds int DEFAULT 15,p_event uuid DEFAULT NULL)
 RETURNS TABLE(event_id uuid,customer_id uuid,generation bigint,owner uuid) LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,banking AS $$
BEGIN
 IF p_owner IS NULL OR p_limit IS NULL OR p_lease_seconds IS NULL OR p_limit NOT BETWEEN 1 AND 32 OR p_lease_seconds NOT BETWEEN 1 AND 60 THEN
  RAISE EXCEPTION 'Invalid bounded claim' USING ERRCODE='22023'; END IF;
 -- No payload is returned to dispatch. Claim commits before computation starts.
 RETURN QUERY WITH candidates AS (
  SELECT o.id FROM banking.outbox_events o WHERE
   o.event_type<>'turn.accepted' AND ((o.status='pending' AND o.available_at<=clock_timestamp()) OR
   (o.status='claimed' AND o.locked_until<=clock_timestamp())) AND (p_event IS NULL OR o.id=p_event)
  ORDER BY o.available_at,o.id FOR UPDATE SKIP LOCKED LIMIT p_limit
 ), retired AS (
  UPDATE banking.outbox_events o SET status='dead_letter',failure_code='retry_exhausted',
   completed_at=clock_timestamp(),locked_until=NULL
  FROM candidates c WHERE o.id=c.id AND o.attempt>=3 RETURNING o.*
 ), terminal AS (
  INSERT INTO banking.consumer_terminal_events(event_id,customer_id,generation,status)
  SELECT r.id,r.customer_id,r.lease_generation,'dead_letter' FROM retired r RETURNING banking.consumer_terminal_events.event_id
 ), claimed AS (
  UPDATE banking.outbox_events o SET status='claimed',attempt=o.attempt+1,
   locked_by=p_owner::text,locked_until=clock_timestamp()+make_interval(secs=>p_lease_seconds),
   lease_generation=o.lease_generation+1,fencing_token=o.fencing_token+1
  FROM candidates c WHERE o.id=c.id AND o.attempt<3 RETURNING o.*
 ) SELECT c.id,c.customer_id,c.lease_generation,p_owner FROM claimed c;
END $$;


