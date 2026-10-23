CREATE FUNCTION banking.claim_turn(p_namespace text) RETURNS banking.turns LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE r banking.turns;
BEGIN
 IF p_namespace IS NULL OR p_namespace !~ '^[a-z0-9_-]{1,60}$' THEN RAISE EXCEPTION 'Invalid namespace' USING ERRCODE='22023';END IF;
 SELECT t.* INTO r FROM banking.turns t JOIN banking.outbox_events o ON o.id=t.outbox_id WHERE split_part(t.session_ref,':',1)=p_namespace AND t.result IS NULL AND o.failure_code IS DISTINCT FROM 'source_deleted' AND (t.lease_until IS NULL OR t.lease_until<=clock_timestamp()) ORDER BY t.accepted_at,t.id FOR UPDATE OF t SKIP LOCKED LIMIT 1;
 IF r.id IS NULL THEN RETURN r;END IF;
 PERFORM set_config('bank.customer_id',r.customer_id::text,true);
 UPDATE banking.account_runs SET generation=generation+1 WHERE id=r.run_id AND result IS NULL;
 UPDATE banking.turns SET generation=generation+1,attempts=attempts+1,lease_until=clock_timestamp()+interval '2 seconds',status='running' WHERE id=r.id RETURNING * INTO r;
 INSERT INTO banking.turn_events SELECT r.id,r.customer_id,coalesce(max(event_id),0)+1,'turn.running',jsonb_build_object('status','running','attempt',r.attempts),clock_timestamp() FROM banking.turn_events WHERE turn_id=r.id;
 RETURN r;
END $$;

REVOKE ALL ON FUNCTION banking.claim_turn() FROM @WORKER@;
REVOKE ALL ON FUNCTION banking.claim_turn(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.claim_turn(text) TO @WORKER@;
