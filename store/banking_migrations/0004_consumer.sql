-- T02.2: restricted durable dispatch and local transactional consumer.
ALTER TABLE banking.outbox_events DROP CONSTRAINT outbox_events_schema_version_check;
ALTER TABLE banking.outbox_events ADD CONSTRAINT outbox_schema_positive CHECK(schema_version>0);
ALTER TABLE banking.outbox_events ADD COLUMN failure_code text;
ALTER TABLE banking.outbox_events ADD COLUMN completed_at timestamptz;
-- Cross-tenant enumeration is available only inside narrowly granted definer functions.
CREATE POLICY consumer_owner ON banking.outbox_events TO @MIGRATOR@ USING(true) WITH CHECK(true);
CREATE TABLE banking.local_operations (
 event_id uuid PRIMARY KEY, customer_id uuid NOT NULL, result jsonb NOT NULL,
 committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK(jsonb_typeof(result)='object' AND pg_column_size(result)<=4096),
 FOREIGN KEY(event_id,customer_id) REFERENCES banking.outbox_events(id,customer_id)
);
CREATE TABLE banking.consumer_checkpoints (
 event_id uuid PRIMARY KEY, customer_id uuid NOT NULL, generation bigint NOT NULL,
 revision bigint NOT NULL, state jsonb NOT NULL,
 CHECK(jsonb_typeof(state)='object' AND pg_column_size(state)<=4096),
 FOREIGN KEY(event_id,customer_id) REFERENCES banking.outbox_events(id,customer_id)
);
CREATE TABLE banking.consumer_terminal_events (
 event_id uuid PRIMARY KEY, customer_id uuid NOT NULL, generation bigint NOT NULL,
 status text NOT NULL CHECK(status IN ('completed','dead_letter')),
 published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(event_id,customer_id) REFERENCES banking.outbox_events(id,customer_id)
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['local_operations','consumer_checkpoints','consumer_terminal_events'] LOOP
  EXECUTE format('ALTER TABLE banking.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE banking.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY tenant_context ON banking.%I USING (customer_id=nullif(current_setting(''bank.customer_id'',true),'''')::uuid) WITH CHECK (customer_id=nullif(current_setting(''bank.customer_id'',true),'''')::uuid)',t);
 END LOOP;
END $$;
CREATE POLICY consumer_owner ON banking.consumer_terminal_events TO @MIGRATOR@ USING(true) WITH CHECK(true);
GRANT USAGE ON SCHEMA banking TO @DISPATCHER@;
GRANT SELECT ON banking.local_operations,banking.consumer_checkpoints,banking.consumer_terminal_events TO @WORKER@;

CREATE FUNCTION banking.claim_outbox(p_owner uuid,p_limit int DEFAULT 16,p_lease_seconds int DEFAULT 15,p_event uuid DEFAULT NULL)
 RETURNS TABLE(event_id uuid,customer_id uuid,generation bigint,owner uuid) LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,banking AS $$
BEGIN
 IF p_owner IS NULL OR p_limit IS NULL OR p_lease_seconds IS NULL OR p_limit NOT BETWEEN 1 AND 32 OR p_lease_seconds NOT BETWEEN 1 AND 60 THEN
  RAISE EXCEPTION 'Invalid bounded claim' USING ERRCODE='22023'; END IF;
 -- No payload is returned to dispatch. Claim commits before computation starts.
 RETURN QUERY WITH candidates AS (
  SELECT o.id FROM banking.outbox_events o WHERE
   ((o.status='pending' AND o.available_at<=clock_timestamp()) OR
   (o.status='claimed' AND o.locked_until<=clock_timestamp())) AND (p_event IS NULL OR o.id=p_event)
  ORDER BY o.available_at,o.id FOR UPDATE SKIP LOCKED LIMIT p_limit
 ), retired AS (
  UPDATE banking.outbox_events o SET status='dead_letter',failure_code='retry_exhausted',
   completed_at=clock_timestamp(),locked_until=NULL
  FROM candidates c WHERE o.id=c.id AND o.attempt>=3 RETURNING o.*
 ), terminal AS (
  INSERT INTO banking.consumer_terminal_events(event_id,customer_id,generation,status)
  SELECT r.id,r.customer_id,r.lease_generation,'dead_letter' FROM retired r RETURNING event_id
 ), claimed AS (
  UPDATE banking.outbox_events o SET status='claimed',attempt=o.attempt+1,
   locked_by=p_owner::text,locked_until=clock_timestamp()+make_interval(secs=>p_lease_seconds),
   lease_generation=o.lease_generation+1,fencing_token=o.fencing_token+1
  FROM candidates c WHERE o.id=c.id AND o.attempt<3 RETURNING o.*
 ) SELECT c.id,c.customer_id,c.lease_generation,p_owner FROM claimed c;
END $$;

-- Private shared ownership lock: every publication and reclaim serializes on outbox row.
CREATE FUNCTION banking.lock_consumer(p_event uuid,p_owner uuid,p_generation bigint)
 RETURNS banking.outbox_events LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE o banking.outbox_events; customer uuid:=nullif(current_setting('bank.customer_id',true),'')::uuid;
BEGIN
 SELECT * INTO o FROM banking.outbox_events WHERE id=p_event AND customer_id=customer FOR UPDATE;
 IF NOT FOUND OR o.status<>'claimed' OR o.locked_by IS DISTINCT FROM p_owner::text
  OR o.lease_generation IS DISTINCT FROM p_generation OR o.fencing_token IS DISTINCT FROM p_generation
  OR o.locked_until IS NULL OR o.locked_until<=clock_timestamp() THEN
  RAISE EXCEPTION 'Stale consumer ownership' USING ERRCODE='42501'; END IF;
 RETURN o;
END $$;
CREATE FUNCTION banking.read_consumer_source(p_event uuid,p_owner uuid,p_generation bigint)
 RETURNS TABLE(event_type text,schema_version int,source_id uuid,source_type text,sequence bigint,payload jsonb)
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE o banking.outbox_events;
BEGIN
 o:=banking.lock_consumer(p_event,p_owner,p_generation);
 RETURN QUERY SELECT o.event_type,o.schema_version,s.id,s.event_type,s.sequence,s.payload
 FROM banking.accepted_user_events s WHERE s.id=o.source_ref AND s.customer_id=o.customer_id;
END $$;
CREATE FUNCTION banking.heartbeat_consumer(p_event uuid,p_owner uuid,p_generation bigint,p_seconds int)
 RETURNS timestamptz LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE o banking.outbox_events; until_time timestamptz;
BEGIN
 IF p_seconds IS NULL OR p_seconds NOT BETWEEN 1 AND 60 THEN RAISE EXCEPTION 'Invalid lease' USING ERRCODE='22023'; END IF;
 o:=banking.lock_consumer(p_event,p_owner,p_generation);
 UPDATE banking.outbox_events SET locked_until=clock_timestamp()+make_interval(secs=>p_seconds)
 WHERE id=o.id RETURNING locked_until INTO until_time;
 RETURN until_time;
END $$;
CREATE FUNCTION banking.checkpoint_consumer(p_event uuid,p_owner uuid,p_generation bigint,p_state jsonb)
 RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE o banking.outbox_events; revision_number bigint;
BEGIN
 o:=banking.lock_consumer(p_event,p_owner,p_generation);
 INSERT INTO banking.consumer_checkpoints(event_id,customer_id,generation,revision,state)
 VALUES(o.id,o.customer_id,p_generation,1,p_state)
 ON CONFLICT(event_id) DO UPDATE SET generation=EXCLUDED.generation,
 revision=banking.consumer_checkpoints.revision+1,state=EXCLUDED.state RETURNING revision INTO revision_number;
 PERFORM banking.lock_consumer(p_event,p_owner,p_generation);
 RETURN revision_number;
END $$;
CREATE FUNCTION banking.complete_consumer(p_event uuid,p_owner uuid,p_generation bigint,p_result jsonb)
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
 UPDATE banking.outbox_events SET status='completed',completed_at=clock_timestamp(),failure_code=NULL WHERE id=o.id;
 RETURN CASE WHEN obtained IS NULL THEN 'duplicate' ELSE 'applied' END;
END $$;
CREATE FUNCTION banking.fail_consumer(p_event uuid,p_owner uuid,p_generation bigint,p_code text)
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
 RETURN next_status;
END $$;
CREATE FUNCTION banking.lookup_consumer(p_event uuid)
 RETURNS TABLE(status text,failure_code text,result jsonb,generation bigint,attempt int)
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
 SELECT o.status,o.failure_code,l.result,o.lease_generation,o.attempt FROM banking.outbox_events o
 LEFT JOIN banking.local_operations l ON l.event_id=o.id AND l.customer_id=o.customer_id
 WHERE o.id=p_event AND o.customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid
$$;
REVOKE ALL ON FUNCTION banking.claim_outbox(uuid,int,int,uuid),banking.lock_consumer(uuid,uuid,bigint),
 banking.read_consumer_source(uuid,uuid,bigint),banking.heartbeat_consumer(uuid,uuid,bigint,int),
 banking.checkpoint_consumer(uuid,uuid,bigint,jsonb),banking.complete_consumer(uuid,uuid,bigint,jsonb),
 banking.fail_consumer(uuid,uuid,bigint,text),banking.lookup_consumer(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.claim_outbox(uuid,int,int,uuid) TO @DISPATCHER@;
GRANT EXECUTE ON FUNCTION banking.read_consumer_source(uuid,uuid,bigint),banking.heartbeat_consumer(uuid,uuid,bigint,int),
 banking.checkpoint_consumer(uuid,uuid,bigint,jsonb),banking.complete_consumer(uuid,uuid,bigint,jsonb),
 banking.fail_consumer(uuid,uuid,bigint,text),banking.lookup_consumer(uuid) TO @WORKER@;
GRANT EXECUTE ON FUNCTION banking.lookup_consumer(uuid) TO @RUNTIME@;
