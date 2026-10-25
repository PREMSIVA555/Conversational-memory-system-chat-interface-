-- T05.2: local confirmation only. No remote support delivery or status advance.
ALTER TABLE banking.support_proposals DROP CONSTRAINT support_proposals_status_check;
ALTER TABLE banking.support_proposals ADD CHECK(status IN ('pending','cancelled','consumed'));
CREATE TABLE banking.support_cases (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), customer_id uuid NOT NULL,
 source_turn_id uuid NOT NULL, proposal_id uuid NOT NULL UNIQUE,
 proposal_version integer NOT NULL, payload_hash text NOT NULL,
 payload jsonb, status text NOT NULL DEFAULT 'created' CHECK(status='created'),
 case_number text GENERATED ALWAYS AS ('SR-'||replace(id::text,'-','')) STORED UNIQUE,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(customer_id,source_turn_id), UNIQUE(id,customer_id),
 FOREIGN KEY(source_turn_id,customer_id) REFERENCES banking.turns(id,customer_id)
);
CREATE TABLE banking.support_operations (
 customer_id uuid NOT NULL, session_binding text NOT NULL, operation_key uuid NOT NULL,
 request_hash text NOT NULL, proposal_id uuid NOT NULL, case_id uuid NOT NULL UNIQUE,
 receipt jsonb NOT NULL, expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '24 hours',
 PRIMARY KEY(customer_id,session_binding,operation_key),
 FOREIGN KEY(case_id,customer_id) REFERENCES banking.support_cases(id,customer_id)
);
CREATE TABLE banking.support_action_audit (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),customer_id uuid NOT NULL,case_id uuid NOT NULL UNIQUE,
 event text NOT NULL CHECK(event='case.created'),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(case_id,customer_id) REFERENCES banking.support_cases(id,customer_id)
);
CREATE TABLE banking.support_outbox (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),customer_id uuid NOT NULL,case_id uuid NOT NULL UNIQUE,
 event_type text NOT NULL DEFAULT 'support.case.created' CHECK(event_type='support.case.created'),
 schema_version integer NOT NULL DEFAULT 1 CHECK(schema_version=1),
 status text NOT NULL DEFAULT 'pending' CHECK(status='pending'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(case_id,customer_id) REFERENCES banking.support_cases(id,customer_id)
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['support_cases','support_operations','support_action_audit','support_outbox'] LOOP
  EXECUTE format('ALTER TABLE banking.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE banking.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY tenant ON banking.%I USING(customer_id=nullif(current_setting(''bank.customer_id'',true),'''')::uuid) WITH CHECK(customer_id=nullif(current_setting(''bank.customer_id'',true),'''')::uuid)',t);
  EXECUTE format('GRANT SELECT ON banking.%I TO @RUNTIME@',t);
  EXECUTE format('CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON banking.%I FOR EACH ROW EXECUTE FUNCTION banking.keep_audit()',t);
 END LOOP;
END $$;
-- Source erasure removes issue text, while non-content uniqueness/audit/receipt
-- metadata remains durable. No FK to deletable draft rows or personal outbox data.
DROP TRIGGER immutable ON banking.support_cases;
CREATE FUNCTION banking.keep_support_case() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,banking AS $$
BEGIN
 -- Stored generated columns are not populated in BEFORE UPDATE's NEW row.
 -- case_number is determined solely by the immutable id; exclude it here.
 IF TG_OP='UPDATE' AND NEW.payload IS NULL AND (to_jsonb(NEW)-'payload'-'case_number')=(to_jsonb(OLD)-'payload'-'case_number')
 AND EXISTS(SELECT 1 FROM banking.turns t JOIN banking.erasure_ledger e ON e.source_id=t.source_id WHERE t.id=OLD.source_turn_id) THEN RETURN NEW; END IF;
 RAISE EXCEPTION 'Immutable support case' USING ERRCODE='42501';
END $$;
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON banking.support_cases FOR EACH ROW EXECUTE FUNCTION banking.keep_support_case();
CREATE FUNCTION banking.erase_support_case_text() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
BEGIN
 UPDATE banking.support_cases c SET payload=NULL FROM banking.turns t
 WHERE c.source_turn_id=t.id AND t.source_id=NEW.source_id AND c.customer_id=NEW.customer_id;
 RETURN NEW;
END $$;
CREATE TRIGGER erase_support_case_text AFTER INSERT ON banking.erasure_ledger FOR EACH ROW EXECUTE FUNCTION banking.erase_support_case_text();
CREATE OR REPLACE FUNCTION banking.keep_support_proposal() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,banking AS $$
BEGIN
 IF TG_OP='DELETE' AND EXISTS(SELECT 1 FROM banking.turns t JOIN banking.erasure_ledger e ON e.source_id=t.source_id WHERE t.id=OLD.source_turn_id) THEN RETURN OLD; END IF;
 IF TG_OP='UPDATE' AND (to_jsonb(NEW)-'status')=(to_jsonb(OLD)-'status') AND OLD.status='pending' THEN
  IF NEW.status='cancelled' THEN RETURN NEW; END IF;
  IF NEW.status='consumed' AND EXISTS(SELECT 1 FROM banking.support_cases c
   JOIN banking.support_operations o ON o.case_id=c.id JOIN banking.support_action_audit a ON a.case_id=c.id
   JOIN banking.support_outbox b ON b.case_id=c.id WHERE c.proposal_id=OLD.id) THEN RETURN NEW; END IF;
 END IF;
 RAISE EXCEPTION 'Immutable support proposal' USING ERRCODE='42501';
END $$;
CREATE FUNCTION banking.commit_support_case(p_id uuid,p_version integer,p_hash text,p_binding text,p_key uuid,p_request_hash text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE p banking.support_proposals; c banking.support_cases; receipt jsonb;
 customer uuid:=nullif(current_setting('bank.customer_id',true),'')::uuid;
BEGIN
 SELECT * INTO p FROM banking.support_proposals WHERE id=p_id AND customer_id=customer AND session_binding=p_binding;
 IF NOT FOUND OR NOT banking.lock_support_source(p.source_turn_id,p_binding) THEN RAISE EXCEPTION 'Not found' USING ERRCODE='P0002'; END IF;
 SELECT * INTO p FROM banking.support_proposals WHERE id=p_id AND customer_id=customer AND session_binding=p_binding FOR UPDATE;
 IF p.status<>'pending' OR p.expires_at<=clock_timestamp() OR p.version<>p_version OR p.payload_hash<>p_hash THEN
  RAISE EXCEPTION 'Preview changed' USING ERRCODE='40001'; END IF;
 IF (p.payload->>'topic'='account' AND NOT EXISTS(SELECT 1 FROM banking.accounts WHERE id=(p.payload->>'resource_id')::uuid AND customer_id=customer))
 OR (p.payload->>'topic'='card' AND NOT EXISTS(SELECT 1 FROM banking.cards WHERE id=(p.payload->>'resource_id')::uuid AND customer_id=customer))
 OR (p.payload->>'topic'='loan' AND NOT EXISTS(SELECT 1 FROM banking.loans WHERE id=(p.payload->>'resource_id')::uuid AND customer_id=customer)) THEN
  RAISE EXCEPTION 'Not found' USING ERRCODE='P0002'; END IF;
 INSERT INTO banking.support_cases(customer_id,source_turn_id,proposal_id,proposal_version,payload_hash,payload)
 VALUES(customer,p.source_turn_id,p.id,p.version,p.payload_hash,p.payload) RETURNING * INTO c;
 receipt:=jsonb_build_object('operation_key',p_key,'case_id',c.id,'case_number',c.case_number,'created_at',c.created_at,'result','case.created');
 INSERT INTO banking.support_action_audit(customer_id,case_id,event) VALUES(customer,c.id,'case.created');
 INSERT INTO banking.support_operations(customer_id,session_binding,operation_key,request_hash,proposal_id,case_id,receipt)
 VALUES(customer,p_binding,p_key,p_request_hash,p.id,c.id,receipt);
 INSERT INTO banking.support_outbox(customer_id,case_id) VALUES(customer,c.id);
 UPDATE banking.support_proposals SET status='consumed' WHERE id=p.id;
 RETURN receipt;
END $$;
REVOKE ALL ON FUNCTION banking.commit_support_case(uuid,integer,text,text,uuid,text),banking.keep_support_case(),banking.erase_support_case_text() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.commit_support_case(uuid,integer,text,text,uuid,text) TO @RUNTIME@;
