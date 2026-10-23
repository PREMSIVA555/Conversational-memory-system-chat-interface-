-- Read terminal worker failure truthfully; no retained active overlay for dead letters.
CREATE FUNCTION banking.preference_work_status(p_source uuid)
 RETURNS TABLE(status text,failure_code text) LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
 SELECT o.status,o.failure_code FROM banking.outbox_events o JOIN banking.preference_proposals p ON p.source_id=o.source_ref
 WHERE o.source_ref=p_source AND o.customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid
$$;
REVOKE ALL ON FUNCTION banking.preference_work_status(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.preference_work_status(uuid) TO @RUNTIME@;
