-- T05.1: immutable drafts only. Case creation belongs to T05.2.
ALTER TABLE banking.turns ADD CONSTRAINT turn_owner_key UNIQUE(id,customer_id);
CREATE TABLE banking.support_proposals (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), customer_id uuid NOT NULL,
 source_turn_id uuid NOT NULL, session_binding text NOT NULL CHECK(session_binding ~ '^[a-f0-9]{64}$'),
 version integer NOT NULL CHECK(version>0), payload jsonb NOT NULL,
 payload_hash text NOT NULL CHECK(payload_hash ~ '^[a-f0-9]{64}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '15 minutes',
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','cancelled')),
 UNIQUE(source_turn_id,version), FOREIGN KEY(source_turn_id,customer_id) REFERENCES banking.turns(id,customer_id),
 CHECK(expires_at>created_at)
);
CREATE UNIQUE INDEX support_one_pending ON banking.support_proposals(source_turn_id) WHERE status='pending';
ALTER TABLE banking.support_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.support_proposals FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant ON banking.support_proposals
 USING(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid
   AND NOT EXISTS(SELECT 1 FROM banking.turns t JOIN banking.erasure_ledger e ON e.source_id=t.source_id WHERE t.id=source_turn_id))
 WITH CHECK(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid
   AND NOT EXISTS(SELECT 1 FROM banking.turns t JOIN banking.erasure_ledger e ON e.source_id=t.source_id WHERE t.id=source_turn_id));
GRANT SELECT,INSERT ON banking.support_proposals TO @RUNTIME@;
GRANT UPDATE(status) ON banking.support_proposals TO @RUNTIME@;
CREATE POLICY proposal_cleanup ON banking.support_proposals TO @MIGRATOR@ USING(true) WITH CHECK(true);
CREATE FUNCTION banking.keep_support_proposal() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,banking AS $$
BEGIN
 IF TG_OP='DELETE' AND EXISTS(SELECT 1 FROM banking.turns t JOIN banking.erasure_ledger e ON e.source_id=t.source_id WHERE t.id=OLD.source_turn_id) THEN
  RETURN OLD;
 END IF;
 IF TG_OP='DELETE' OR (to_jsonb(NEW)-'status') IS DISTINCT FROM (to_jsonb(OLD)-'status')
    OR OLD.status<>'pending' OR NEW.status<>'cancelled' THEN
  RAISE EXCEPTION 'Immutable support proposal' USING ERRCODE='42501';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER keep_support_proposal BEFORE UPDATE OR DELETE ON banking.support_proposals
 FOR EACH ROW EXECUTE FUNCTION banking.keep_support_proposal();
REVOKE ALL ON FUNCTION banking.keep_support_proposal() FROM PUBLIC;
-- Erasure removes draft text as well as blocking serving. A future committed case
-- needs its own explicit retention contract; no case store exists in this slice.
CREATE FUNCTION banking.erase_support_proposals() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
BEGIN
 DELETE FROM banking.support_proposals p USING banking.turns t
 WHERE p.source_turn_id=t.id AND t.source_id=NEW.source_id AND p.customer_id=NEW.customer_id;
 RETURN NEW;
END $$;
CREATE TRIGGER erase_support_proposals AFTER INSERT ON banking.erasure_ledger
 FOR EACH ROW EXECUTE FUNCTION banking.erase_support_proposals();
REVOKE ALL ON FUNCTION banking.erase_support_proposals() FROM PUBLIC;
