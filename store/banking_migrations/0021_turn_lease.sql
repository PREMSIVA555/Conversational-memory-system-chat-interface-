-- Short renewable leases permit process-crash recovery within original UTC budget.
CREATE OR REPLACE FUNCTION banking.claim_turn() RETURNS banking.turns LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE r banking.turns;
BEGIN
 SELECT t.* INTO r FROM banking.turns t JOIN banking.outbox_events o ON o.id=t.outbox_id WHERE t.result IS NULL AND o.failure_code IS DISTINCT FROM 'source_deleted' AND (t.lease_until IS NULL OR t.lease_until<=clock_timestamp()) ORDER BY t.accepted_at,t.id FOR UPDATE OF t SKIP LOCKED LIMIT 1;
 IF r.id IS NULL THEN RETURN r;END IF;
 PERFORM set_config('bank.customer_id',r.customer_id::text,true);
 UPDATE banking.account_runs SET generation=generation+1 WHERE id=r.run_id AND result IS NULL;
 UPDATE banking.turns SET generation=generation+1,attempts=attempts+1,lease_until=clock_timestamp()+interval '2 seconds',status='running' WHERE id=r.id RETURNING * INTO r;
 INSERT INTO banking.turn_events SELECT r.id,r.customer_id,coalesce(max(event_id),0)+1,'turn.running',jsonb_build_object('status','running','attempt',r.attempts),clock_timestamp() FROM banking.turn_events WHERE turn_id=r.id;
 RETURN r;
END $$;
CREATE FUNCTION banking.renew_turn(p_id uuid,p_generation bigint) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
BEGIN
 UPDATE banking.turns SET lease_until=clock_timestamp()+interval '2 seconds' WHERE id=p_id AND generation=p_generation AND result IS NULL AND lease_until>clock_timestamp();
 RETURN FOUND;
END $$;
REVOKE ALL ON FUNCTION banking.renew_turn(uuid,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.renew_turn(uuid,bigint) TO @WORKER@;
