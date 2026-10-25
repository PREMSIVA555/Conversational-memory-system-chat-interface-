-- T05.3: durable request identity and separately checked current case status.
CREATE TABLE banking.support_submission_intents (
 proposal_id uuid PRIMARY KEY,customer_id uuid NOT NULL,source_turn_id uuid NOT NULL,
 session_binding text NOT NULL,operation_key uuid NOT NULL,request_hash text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(customer_id,session_binding,operation_key),
 FOREIGN KEY(source_turn_id,customer_id) REFERENCES banking.turns(id,customer_id)
);
ALTER TABLE banking.support_submission_intents ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.support_submission_intents FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant ON banking.support_submission_intents
 USING(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid)
 WITH CHECK(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid);
GRANT SELECT ON banking.support_submission_intents TO @RUNTIME@;
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON banking.support_submission_intents
 FOR EACH ROW EXECUTE FUNCTION banking.keep_audit();
-- Preserve legacy committed request identities without copying issue text.
CREATE POLICY migration_backfill ON banking.support_submission_intents TO @MIGRATOR@ USING(true) WITH CHECK(true);
CREATE POLICY continuity_backfill_ops ON banking.support_operations TO @MIGRATOR@ USING(true);
CREATE POLICY continuity_backfill_cases ON banking.support_cases TO @MIGRATOR@ USING(true);
INSERT INTO banking.support_submission_intents(proposal_id,customer_id,source_turn_id,session_binding,operation_key,request_hash)
 SELECT o.proposal_id,o.customer_id,c.source_turn_id,o.session_binding,o.operation_key,o.request_hash
 FROM banking.support_operations o JOIN banking.support_cases c ON c.id=o.case_id;
DROP POLICY migration_backfill ON banking.support_submission_intents;
DROP POLICY continuity_backfill_ops ON banking.support_operations;
DROP POLICY continuity_backfill_cases ON banking.support_cases;
CREATE FUNCTION banking.reserve_support_submission(p_id uuid,p_version integer,p_hash text,p_binding text,p_key uuid,p_request_hash text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE p banking.support_proposals;i banking.support_submission_intents;
 customer uuid:=nullif(current_setting('bank.customer_id',true),'')::uuid;
BEGIN
 SELECT * INTO p FROM banking.support_proposals WHERE id=p_id AND customer_id=customer AND session_binding=p_binding;
 IF NOT FOUND OR NOT banking.lock_support_source(p.source_turn_id,p_binding) THEN RAISE EXCEPTION 'Not found' USING ERRCODE='P0002'; END IF;
 SELECT * INTO p FROM banking.support_proposals WHERE id=p_id AND customer_id=customer AND session_binding=p_binding FOR UPDATE;
 IF p.status<>'pending' OR p.expires_at<=clock_timestamp() OR p.version<>p_version OR p.payload_hash<>p_hash THEN
  RAISE EXCEPTION 'Preview changed' USING ERRCODE='40001'; END IF;
 SELECT * INTO i FROM banking.support_submission_intents WHERE proposal_id=p.id;
 IF FOUND THEN
  IF i.operation_key IS DISTINCT FROM p_key OR i.request_hash IS DISTINCT FROM p_request_hash OR i.session_binding IS DISTINCT FROM p_binding THEN
   RAISE EXCEPTION 'Original request required' USING ERRCODE='23505'; END IF;
  RETURN;
 END IF;
 INSERT INTO banking.support_submission_intents(proposal_id,customer_id,source_turn_id,session_binding,operation_key,request_hash)
 VALUES(p.id,customer,p.source_turn_id,p_binding,p_key,p_request_hash);
END $$;
REVOKE ALL ON FUNCTION banking.reserve_support_submission(uuid,integer,text,text,uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.reserve_support_submission(uuid,integer,text,text,uuid,text) TO @RUNTIME@;

-- Current status is mutable only through the migrator/fixture seam, never an API/model.
DROP TRIGGER immutable ON banking.support_cases;
ALTER TABLE banking.support_cases DROP CONSTRAINT support_cases_status_check;
ALTER TABLE banking.support_cases ADD CHECK(status IN ('created','in_review','resolved'));
ALTER TABLE banking.support_cases ADD COLUMN status_updated_at timestamptz;
CREATE POLICY continuity_status_backfill ON banking.support_cases TO @MIGRATOR@ USING(true) WITH CHECK(true);
UPDATE banking.support_cases SET status_updated_at=created_at;
DROP POLICY continuity_status_backfill ON banking.support_cases;
ALTER TABLE banking.support_cases ALTER COLUMN status_updated_at SET NOT NULL;
ALTER TABLE banking.support_cases ALTER COLUMN status_updated_at SET DEFAULT clock_timestamp();
CREATE OR REPLACE FUNCTION banking.keep_support_case() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,banking AS $$
BEGIN
 IF TG_OP='UPDATE' AND NEW.payload IS NULL AND (to_jsonb(NEW)-'payload'-'case_number')=(to_jsonb(OLD)-'payload'-'case_number')
 AND EXISTS(SELECT 1 FROM banking.turns t JOIN banking.erasure_ledger e ON e.source_id=t.source_id WHERE t.id=OLD.source_turn_id) THEN RETURN NEW; END IF;
 IF TG_OP='UPDATE' AND current_user::regrole='@MIGRATOR@'::regrole
 AND (to_jsonb(NEW)-'status'-'status_updated_at'-'case_number')=(to_jsonb(OLD)-'status'-'status_updated_at'-'case_number')
 AND NEW.status_updated_at>=OLD.status_updated_at
 AND ((OLD.status='created' AND NEW.status IN ('in_review','resolved')) OR (OLD.status='in_review' AND NEW.status='resolved')) THEN RETURN NEW; END IF;
 RAISE EXCEPTION 'Immutable support case' USING ERRCODE='42501';
END $$;
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON banking.support_cases FOR EACH ROW EXECUTE FUNCTION banking.keep_support_case();
CREATE TABLE banking.support_case_status_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),customer_id uuid NOT NULL,case_id uuid NOT NULL,
 status text NOT NULL CHECK(status IN ('in_review','resolved')),changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(case_id,customer_id) REFERENCES banking.support_cases(id,customer_id)
);
ALTER TABLE banking.support_case_status_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.support_case_status_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant ON banking.support_case_status_events USING(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid)
 WITH CHECK(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON banking.support_case_status_events FOR EACH ROW EXECUTE FUNCTION banking.keep_audit();
CREATE FUNCTION banking.fixture_advance_support_case(p_id uuid,p_status text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE c banking.support_cases;
BEGIN
 SELECT * INTO c FROM banking.support_cases WHERE id=p_id AND customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Not found' USING ERRCODE='P0002'; END IF;
 IF c.status=p_status THEN RETURN; END IF;
 UPDATE banking.support_cases SET status=p_status,status_updated_at=clock_timestamp() WHERE id=c.id;
 INSERT INTO banking.support_case_status_events(customer_id,case_id,status) VALUES(c.customer_id,c.id,p_status);
END $$;
REVOKE ALL ON FUNCTION banking.fixture_advance_support_case(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.fixture_advance_support_case(uuid,text) TO @MIGRATOR@;
