-- Explicit local retention metadata; cleanup is customer-triggered, not an invented background sweep.
ALTER TABLE banking.working_context ADD COLUMN expires_at timestamptz NOT NULL DEFAULT (clock_timestamp()+interval '30 minutes');
ALTER TABLE banking.quarantined_memory_backups ADD COLUMN expires_at timestamptz NOT NULL DEFAULT (clock_timestamp()+interval '24 hours');
CREATE OR REPLACE FUNCTION banking.write_context(p_session text,p_conversation uuid,p_version bigint,p_selections jsonb)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE customer uuid:=nullif(current_setting('bank.customer_id',true),'')::uuid; current_version bigint; item text;
BEGIN
 PERFORM 1 FROM banking.customers WHERE customer_id=customer FOR UPDATE;
 IF NOT FOUND OR length(p_session)<>64 THEN RAISE EXCEPTION 'Invalid context' USING ERRCODE='42501'; END IF;
 IF NOT EXISTS(SELECT 1 FROM banking.conversations WHERE id=p_conversation AND customer_id=customer) THEN
 RAISE EXCEPTION 'Missing conversation' USING ERRCODE='P0002'; END IF;
 IF jsonb_typeof(p_selections)<>'array' OR jsonb_array_length(p_selections)>8 THEN
 RAISE EXCEPTION 'Invalid selections' USING ERRCODE='22023'; END IF;
 FOR item IN SELECT jsonb_array_elements_text(p_selections) LOOP
  IF item !~ '^conversation:[0-9a-f-]{36}$' OR NOT EXISTS(SELECT 1 FROM banking.conversations
   WHERE id=substring(item from 14)::uuid AND customer_id=customer) THEN
   RAISE EXCEPTION 'Selection unavailable' USING ERRCODE='P0002'; END IF;
 END LOOP;
 SELECT version INTO current_version FROM banking.working_context WHERE customer_id=customer
 AND session_binding=p_session AND conversation_id=p_conversation AND expires_at>clock_timestamp();
 IF coalesce(current_version,0)<>p_version THEN RAISE EXCEPTION 'Stale context' USING ERRCODE='40001'; END IF;
 INSERT INTO banking.working_context(customer_id,session_binding,conversation_id,version,selections) VALUES(customer,p_session,p_conversation,p_version+1,p_selections)
 ON CONFLICT(customer_id,session_binding,conversation_id) DO UPDATE SET version=EXCLUDED.version,selections=EXCLUDED.selections,expires_at=clock_timestamp()+interval '30 minutes';
 RETURN jsonb_build_object('version',p_version+1,'selections',p_selections,'conversation_id',p_conversation);
END $$;
CREATE FUNCTION banking.memory_retention() RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE customer uuid:=nullif(current_setting('bank.customer_id',true),'')::uuid; expired int;
BEGIN
 IF NOT EXISTS(SELECT 1 FROM banking.customers WHERE customer_id=customer) THEN RAISE EXCEPTION 'Missing context' USING ERRCODE='42501'; END IF;
 DELETE FROM banking.working_context WHERE customer_id=customer AND expires_at<=clock_timestamp();
 DELETE FROM banking.quarantined_memory_backups WHERE customer_id=customer AND expires_at<=clock_timestamp();
 GET DIAGNOSTICS expired=ROW_COUNT;
 RETURN jsonb_build_object('owner','authenticated_customer','purpose','optional_user_history_and_working_context',
 'working_context_ttl_seconds',1800,'context_selection_limit',8,'history_result_limit',32,'derivative_text_limit',2000,
 'quarantined_backup_ttl_seconds',86400,'backup_cleanup_trigger','authenticated_retention_request',
 'expired_backups_removed',expired,'history_retention','until_customer_forgets_source',
 'ordering_retention','minimal_ids_and_sequence_without_deleted_content',
 'external_provider','not_configured','semantic_retrieval','disabled',
 'production_backups','not_managed_by_this_local_fixture');
END $$;
REVOKE ALL ON FUNCTION banking.memory_retention() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.memory_retention() TO @RUNTIME@;
