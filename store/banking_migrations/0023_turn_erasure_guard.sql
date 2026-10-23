-- The existing erasure ledger also fences newly introduced internal-run derivatives.
CREATE FUNCTION banking.guard_erased_turn_run() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM banking.turns t JOIN banking.erasure_ledger e ON e.source_id=t.source_id WHERE t.run_id=NEW.id) THEN
  RAISE EXCEPTION 'Source erased' USING ERRCODE='55000';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER erased_turn_run BEFORE UPDATE ON banking.account_runs FOR EACH ROW EXECUTE FUNCTION banking.guard_erased_turn_run();
REVOKE ALL ON FUNCTION banking.guard_erased_turn_run() FROM PUBLIC;
