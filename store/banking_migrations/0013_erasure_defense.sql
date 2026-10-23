-- Forward correction: table-specific trigger records and generic-effect scrubbing.
CREATE OR REPLACE FUNCTION banking.erase_memory_source(p_source uuid)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE customer uuid:=nullif(current_setting('bank.customer_id',true),'')::uuid;
BEGIN
 PERFORM 1 FROM banking.customers WHERE customer_id=customer FOR UPDATE;
 IF NOT EXISTS(SELECT 1 FROM banking.accepted_user_events WHERE id=p_source AND customer_id=customer) THEN
 RAISE EXCEPTION 'Missing source' USING ERRCODE='P0002'; END IF;
 INSERT INTO banking.quarantined_memory_backups(source_id,customer_id,snapshot)
 SELECT p_source,customer,jsonb_build_object('source',s.payload,'derivatives',coalesce((SELECT jsonb_agg(to_jsonb(d))
 FROM banking.memory_derivatives d WHERE d.id IN(SELECT derivative_id FROM banking.memory_lineage WHERE source_user_event_id=p_source)),'[]'))
 FROM banking.accepted_user_events s WHERE s.id=p_source ON CONFLICT DO NOTHING;
 INSERT INTO banking.erasure_ledger(source_id,customer_id) VALUES(p_source,customer) ON CONFLICT DO NOTHING;
 UPDATE banking.memory_derivatives SET content=NULL,deleted_at=clock_timestamp()
 WHERE customer_id=customer AND id IN(SELECT derivative_id FROM banking.memory_lineage WHERE source_user_event_id=p_source);
 UPDATE banking.memory_lineage SET suppressed_at=clock_timestamp(),cleanup_status='deleted' WHERE derivative_id IN
 (SELECT derivative_id FROM banking.memory_lineage WHERE source_user_event_id=p_source);
 UPDATE banking.preferences_current SET value=NULL,tombstoned=true,version=version+1 WHERE source_event_id IN
 (SELECT id FROM banking.preference_events WHERE source_user_event_id=p_source) AND NOT tombstoned;
 UPDATE banking.preference_events SET operation='DELETE',normalized_value=NULL,source_span=NULL WHERE source_user_event_id=p_source;
 UPDATE banking.preference_proposals SET proposals='[]',status='failed',failure_code='source_deleted' WHERE source_id=p_source;
 UPDATE banking.accepted_user_events SET payload='{}' WHERE id=p_source;
 UPDATE banking.outbox_events SET minimal_payload='{}',trace_context='{}',status='dead_letter',failure_code='source_deleted',
 lease_generation=lease_generation+1,fencing_token=fencing_token+1,locked_by=NULL,locked_until=NULL WHERE source_ref=p_source;
 DELETE FROM banking.local_operations WHERE event_id IN(SELECT id FROM banking.outbox_events WHERE source_ref=p_source);
 DELETE FROM banking.consumer_checkpoints WHERE event_id IN(SELECT id FROM banking.outbox_events WHERE source_ref=p_source);
 RETURN jsonb_build_object('source_id',p_source,'active_block',true,'status','deleted','stores',jsonb_build_object(
 'postgresql','cleaned','derivatives','cleaned','checkpoints','cleaned','redis','no_memory_content_stored',
 'backups','retained_quarantined_restore_scrub_required','external_provider','not_configured','published_evidence','synthetic_evidence_retained'));
END $$;
CREATE OR REPLACE FUNCTION banking.reject_erased_work() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE source uuid;
BEGIN
 CASE TG_TABLE_NAME
 WHEN 'preference_events' THEN source:=NEW.source_user_event_id;
 WHEN 'preference_proposals' THEN source:=NEW.source_id;
 WHEN 'memory_lineage' THEN source:=NEW.source_user_event_id;
 WHEN 'accepted_user_events' THEN source:=NEW.id;
 WHEN 'outbox_events' THEN source:=NEW.source_ref;
 ELSE SELECT source_ref INTO source FROM banking.outbox_events WHERE id=NEW.event_id;
 END CASE;
 IF EXISTS(SELECT 1 FROM banking.erasure_ledger WHERE source_id=source) THEN
  CASE TG_TABLE_NAME
  WHEN 'accepted_user_events' THEN IF NEW.payload='{}'::jsonb THEN RETURN NEW; END IF;
  WHEN 'preference_events' THEN IF NEW.operation='DELETE' AND NEW.normalized_value IS NULL AND NEW.source_span IS NULL THEN RETURN NEW; END IF;
  WHEN 'preference_proposals' THEN IF NEW.proposals='[]'::jsonb AND NEW.failure_code='source_deleted' THEN RETURN NEW; END IF;
  WHEN 'memory_lineage' THEN IF NEW.cleanup_status='deleted' AND NEW.suppressed_at IS NOT NULL THEN RETURN NEW; END IF;
  WHEN 'outbox_events' THEN IF NEW.status='dead_letter' AND NEW.failure_code='source_deleted' AND NEW.minimal_payload='{}'::jsonb AND NEW.trace_context='{}'::jsonb THEN RETURN NEW; END IF;
  ELSE NULL;
  END CASE;
  RAISE EXCEPTION 'Source erased' USING ERRCODE='55000';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER blocked_source BEFORE INSERT OR UPDATE ON banking.local_operations FOR EACH ROW EXECUTE FUNCTION banking.reject_erased_work();
