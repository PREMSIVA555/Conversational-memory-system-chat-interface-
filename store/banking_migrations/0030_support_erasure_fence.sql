-- T05.1 forward correction. Keep the preserved0028 proposal schema immutable.
-- Erasure already locks this customer row before inserting its ledger record.
-- Preview mutations take the same lock before checking source/session/erasure,
-- so cleanup cannot finish before a concurrent new preview version commits.
CREATE FUNCTION banking.lock_support_source(p_turn uuid,p_binding text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE customer uuid:=nullif(current_setting('bank.customer_id',true),'')::uuid;
BEGIN
 IF customer IS NULL THEN RETURN false; END IF;
 PERFORM 1 FROM banking.customers WHERE customer_id=customer FOR UPDATE;
 RETURN EXISTS(SELECT 1 FROM banking.turns t
  WHERE t.id=p_turn AND t.customer_id=customer AND t.session_binding=p_binding
   AND NOT EXISTS(SELECT 1 FROM banking.erasure_ledger e WHERE e.source_id=t.source_id));
END $$;
REVOKE ALL ON FUNCTION banking.lock_support_source(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.lock_support_source(uuid,text) TO @RUNTIME@;
-- Repair any residue left by the old race. The existing immutable-row trigger
-- permits deletion only for ledger-erased sources; no live preview is removed.
DO $$
DECLARE owner uuid; previous_context text:=current_setting('bank.customer_id',true);
BEGIN
 FOR owner IN SELECT DISTINCT customer_id FROM banking.support_proposals LOOP
  PERFORM set_config('bank.customer_id',owner::text,true);
  DELETE FROM banking.support_proposals p USING banking.turns t,banking.erasure_ledger e
   WHERE p.customer_id=owner AND p.source_turn_id=t.id AND t.source_id=e.source_id;
 END LOOP;
 PERFORM set_config('bank.customer_id',coalesce(previous_context,''),true);
END $$;
