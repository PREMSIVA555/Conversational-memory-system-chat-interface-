-- Recovery can resume durable accepted work after logout; pending overlays remain
-- bound to the accepting session and no old text is returned by this capability.
CREATE FUNCTION banking.pending_preference_work() RETURNS TABLE(event_id uuid)
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
 SELECT o.id FROM banking.outbox_events o JOIN banking.preference_proposals p ON p.source_id=o.source_ref
 JOIN banking.accepted_user_events s ON s.id=o.source_ref
 WHERE o.customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid AND p.status='pending' AND
 ((o.status='pending' AND o.available_at<=clock_timestamp()) OR
  (o.status='claimed' AND o.locked_until<=clock_timestamp())) ORDER BY s.sequence LIMIT 16
$$;
REVOKE ALL ON FUNCTION banking.pending_preference_work() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.pending_preference_work() TO @RUNTIME@;
