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
   ((o.status='pending' AND o.available_at<=clock_timestamp()) OR
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

