-- Fence the previous internal generation as soon as its durable turn is reclaimed.
CREATE OR REPLACE FUNCTION banking.claim_turn() RETURNS banking.turns LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE r banking.turns;
BEGIN
 SELECT t.* INTO r FROM banking.turns t JOIN banking.outbox_events o ON o.id=t.outbox_id WHERE t.result IS NULL AND o.failure_code IS DISTINCT FROM 'source_deleted' AND (t.lease_until IS NULL OR t.lease_until<=clock_timestamp()) ORDER BY t.accepted_at,t.id FOR UPDATE OF t SKIP LOCKED LIMIT 1;
 IF r.id IS NULL THEN RETURN r;END IF;
 PERFORM set_config('bank.customer_id',r.customer_id::text,true);
 UPDATE banking.account_runs SET generation=generation+1 WHERE id=r.run_id AND result IS NULL;
 UPDATE banking.turns SET generation=generation+1,attempts=attempts+1,lease_until=clock_timestamp()+interval '35 seconds',status='running' WHERE id=r.id RETURNING * INTO r;
 INSERT INTO banking.turn_events SELECT r.id,r.customer_id,coalesce(max(event_id),0)+1,'turn.running',jsonb_build_object('status','running','attempt',r.attempts),clock_timestamp() FROM banking.turn_events WHERE turn_id=r.id;
 RETURN r;
END $$;
CREATE OR REPLACE FUNCTION banking.finish_turn(p_id uuid,p_generation bigint,p_result jsonb) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE r banking.turns;kind text;
BEGIN
 SELECT * INTO r FROM banking.turns WHERE id=p_id FOR UPDATE;
 IF r.id IS NULL OR r.generation<>p_generation OR r.result IS NOT NULL OR r.lease_until<=clock_timestamp() THEN RETURN false;END IF;
 IF EXISTS(SELECT 1 FROM banking.erasure_ledger WHERE source_id=r.source_id) THEN RETURN false;END IF;
 IF NOT EXISTS(SELECT 1 FROM banking.account_runs WHERE id=r.run_id AND result=p_result) THEN
  IF p_result->>'status' NOT IN ('session_expired','expired','unavailable') OR p_result->'evidence' IS DISTINCT FROM '[]'::jsonb OR (SELECT count(*) FROM jsonb_object_keys(p_result))<>3 OR p_result->>'text' NOT IN
   ('The accepting session expired. Start a new enquiry after signing in.','This enquiry exceeded its execution deadline. Please start a new enquiry.','Current bank information is unavailable. Please try again later.','The selected account changed. Please select it again.') THEN RETURN false;END IF;
 END IF;
 UPDATE banking.turns SET result=p_result,status=p_result->>'status',lease_until=NULL WHERE id=p_id;
 kind:=CASE WHEN p_result->>'status' IN ('expired','unavailable','session_expired') THEN 'turn.failed' ELSE 'answer.ready' END;
 INSERT INTO banking.turn_events SELECT r.id,r.customer_id,coalesce(max(event_id),0)+1,kind,jsonb_build_object('status',p_result->>'status','result',p_result),clock_timestamp() FROM banking.turn_events WHERE turn_id=r.id;
 UPDATE banking.outbox_events SET status='completed',completed_at=clock_timestamp() WHERE id=r.outbox_id;
 RETURN true;
END $$;
