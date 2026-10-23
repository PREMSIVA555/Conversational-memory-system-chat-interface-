-- T02.4: active serving denial precedes acknowledgement; no legacy cutover.
CREATE TABLE banking.working_context (
 customer_id uuid NOT NULL, session_binding text NOT NULL CHECK(length(session_binding)=64),
 conversation_id uuid NOT NULL, version bigint NOT NULL CHECK(version>0), selections jsonb NOT NULL,
 PRIMARY KEY(customer_id,session_binding,conversation_id),
 FOREIGN KEY(conversation_id,customer_id) REFERENCES banking.conversations(id,customer_id)
);
CREATE TABLE banking.erasure_ledger (
 source_id uuid PRIMARY KEY, customer_id uuid NOT NULL, blocked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(source_id,customer_id) REFERENCES banking.accepted_user_events(id,customer_id)
);
-- A deliberately quarantined local restore fixture, never a serving source.
-- Content is retained here as a backup; active erasure does not claim its removal.
CREATE TABLE banking.quarantined_memory_backups (
 source_id uuid PRIMARY KEY, customer_id uuid NOT NULL,
 snapshot jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(source_id,customer_id) REFERENCES banking.accepted_user_events(id,customer_id)
);
ALTER TABLE banking.memory_derivatives ADD COLUMN conversation_id uuid;
ALTER TABLE banking.memory_derivatives ADD COLUMN content text;
ALTER TABLE banking.memory_derivatives ADD COLUMN deleted_at timestamptz;
ALTER TABLE banking.memory_derivatives ADD CONSTRAINT memory_conversation_owner
 FOREIGN KEY(conversation_id,customer_id) REFERENCES banking.conversations(id,customer_id);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['working_context','erasure_ledger','quarantined_memory_backups'] LOOP
 EXECUTE format('ALTER TABLE banking.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE banking.%I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('CREATE POLICY tenant_context ON banking.%I USING(customer_id=nullif(current_setting(''bank.customer_id'',true),'''')::uuid) WITH CHECK(customer_id=nullif(current_setting(''bank.customer_id'',true),'''')::uuid)',t);
 END LOOP;
END $$;
GRANT SELECT ON banking.working_context,banking.erasure_ledger TO @RUNTIME@,@WORKER@;

CREATE FUNCTION banking.write_context(p_session text,p_conversation uuid,p_version bigint,p_selections jsonb)
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
 AND session_binding=p_session AND conversation_id=p_conversation;
 IF coalesce(current_version,0)<>p_version THEN RAISE EXCEPTION 'Stale context' USING ERRCODE='40001'; END IF;
 INSERT INTO banking.working_context VALUES(customer,p_session,p_conversation,p_version+1,p_selections)
 ON CONFLICT(customer_id,session_binding,conversation_id) DO UPDATE SET version=EXCLUDED.version,selections=EXCLUDED.selections;
 RETURN jsonb_build_object('version',p_version+1,'selections',p_selections,'conversation_id',p_conversation);
END $$;

CREATE FUNCTION banking.create_memory(p_client uuid,p_conversation uuid,p_text text)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE customer uuid:=nullif(current_setting('bank.customer_id',true),'')::uuid; source banking.accepted_user_events; memory_id uuid;
BEGIN
 IF length(p_text) NOT BETWEEN 1 AND 2000 OR p_conversation IS NULL THEN
 RAISE EXCEPTION 'Invalid memory' USING ERRCODE='22023'; END IF;
 PERFORM 1 FROM banking.customers WHERE customer_id=customer FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Missing context' USING ERRCODE='42501'; END IF;
 SELECT * INTO source FROM banking.accepted_user_events WHERE customer_id=customer AND client_event_id=p_client;
 IF FOUND THEN
  IF EXISTS(SELECT 1 FROM banking.erasure_ledger WHERE source_id=source.id) THEN
   RAISE EXCEPTION 'Source erased' USING ERRCODE='55000'; END IF;
  IF source.payload IS DISTINCT FROM jsonb_build_object('handler','memory.v1','text',p_text) OR source.conversation_id IS DISTINCT FROM p_conversation THEN
   RAISE EXCEPTION 'Idempotency conflict' USING ERRCODE='23505'; END IF;
  SELECT derivative_id INTO memory_id FROM banking.memory_lineage WHERE source_user_event_id=source.id LIMIT 1;
 ELSE
  IF NOT EXISTS(SELECT 1 FROM banking.conversations WHERE id=p_conversation AND customer_id=customer) THEN
   RAISE EXCEPTION 'Missing conversation' USING ERRCODE='P0002'; END IF;
  UPDATE banking.customers SET acceptance_sequence=acceptance_sequence+1 WHERE customer_id=customer RETURNING acceptance_sequence INTO source.sequence;
  INSERT INTO banking.accepted_user_events(customer_id,actor_ref,sequence,event_type,conversation_id,client_event_id,payload)
   VALUES(customer,'user',source.sequence,'chat',p_conversation,p_client,jsonb_build_object('handler','memory.v1','text',p_text)) RETURNING * INTO source;
  INSERT INTO banking.outbox_events(customer_id,source_ref) VALUES(customer,source.id);
  INSERT INTO banking.memory_derivatives(customer_id,kind,conversation_id,content) VALUES(customer,'memory',p_conversation,p_text) RETURNING id INTO memory_id;
  INSERT INTO banking.memory_lineage(derivative_id,customer_id,source_user_event_id,derivative_kind) VALUES(memory_id,customer,source.id,'memory');
 END IF;
 RETURN jsonb_build_object('id',memory_id,'source_id',source.id,'status','persisted','conversation_id',p_conversation);
END $$;

CREATE FUNCTION banking.derive_memory(p_sources uuid[],p_kind text,p_conversation uuid,p_text text)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE customer uuid:=nullif(current_setting('bank.customer_id',true),'')::uuid; memory_id uuid; source uuid;
BEGIN
 PERFORM 1 FROM banking.customers WHERE customer_id=customer FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Missing context' USING ERRCODE='42501'; END IF;
 IF cardinality(p_sources) NOT BETWEEN 1 AND 8 OR length(p_text) NOT BETWEEN 1 AND 2000 OR p_kind NOT IN ('memory','summary','vector','cache','checkpoint') THEN
 RAISE EXCEPTION 'Invalid derivative' USING ERRCODE='22023'; END IF;
 IF NOT EXISTS(SELECT 1 FROM banking.conversations WHERE id=p_conversation AND customer_id=customer) THEN
 RAISE EXCEPTION 'Missing conversation' USING ERRCODE='P0002'; END IF;
 FOREACH source IN ARRAY p_sources LOOP
  IF NOT EXISTS(SELECT 1 FROM banking.accepted_user_events WHERE id=source AND customer_id=customer AND conversation_id=p_conversation) THEN
   RAISE EXCEPTION 'Source unavailable' USING ERRCODE='P0002'; END IF;
  IF EXISTS(SELECT 1 FROM banking.erasure_ledger WHERE source_id=source) THEN RAISE EXCEPTION 'Source erased' USING ERRCODE='55000'; END IF;
 END LOOP;
 INSERT INTO banking.memory_derivatives(customer_id,kind,conversation_id,content) VALUES(customer,p_kind,p_conversation,p_text) RETURNING id INTO memory_id;
 FOREACH source IN ARRAY p_sources LOOP
 INSERT INTO banking.memory_lineage(derivative_id,customer_id,source_user_event_id,derivative_kind) VALUES(memory_id,customer,source,p_kind) ON CONFLICT DO NOTHING;
 END LOOP;
 RETURN jsonb_build_object('id',memory_id,'kind',p_kind,'status','persisted','source_ids',to_jsonb(p_sources));
END $$;

CREATE FUNCTION banking.read_memories(p_conversation uuid,p_query text,p_id uuid DEFAULT NULL)
 RETURNS TABLE(id uuid,kind text,content text,conversation_id uuid,source_ids uuid[])
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
 SELECT d.id,d.kind,d.content,d.conversation_id,array_agg(l.source_user_event_id ORDER BY l.source_user_event_id)
 FROM banking.memory_derivatives d JOIN banking.memory_lineage l ON l.derivative_id=d.id AND l.customer_id=d.customer_id
 WHERE d.customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid AND d.deleted_at IS NULL
 AND (p_id IS NULL OR d.id=p_id) AND (p_conversation IS NULL OR d.conversation_id=p_conversation)
 AND (p_query IS NULL OR d.content ILIKE '%'||p_query||'%')
 AND NOT EXISTS(SELECT 1 FROM banking.memory_lineage bad JOIN banking.erasure_ledger e ON e.source_id=bad.source_user_event_id
 WHERE bad.derivative_id=d.id)
 GROUP BY d.id ORDER BY d.id LIMIT 32
$$;

CREATE FUNCTION banking.erase_memory_source(p_source uuid)
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
 DELETE FROM banking.consumer_checkpoints WHERE event_id IN(SELECT id FROM banking.outbox_events WHERE source_ref=p_source);
 RETURN jsonb_build_object('source_id',p_source,'active_block',true,'status','deleted','stores',jsonb_build_object(
 'postgresql','cleaned','derivatives','cleaned','checkpoints','cleaned','redis','no_memory_content_stored',
 'backups','retained_quarantined_restore_scrub_required','external_provider','not_configured','published_evidence','synthetic_evidence_retained'));
END $$;

-- Defense in depth: old workers, including generic consumer publications, cannot
-- mutate blocked-source state after erasure. Erasure itself sets the transaction flag.
CREATE FUNCTION banking.reject_erased_work() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE source uuid;
BEGIN
 IF TG_TABLE_NAME='preference_events' THEN source:=NEW.source_user_event_id;
 ELSIF TG_TABLE_NAME='preference_proposals' THEN source:=NEW.source_id;
 ELSIF TG_TABLE_NAME='memory_lineage' THEN source:=NEW.source_user_event_id;
 ELSIF TG_TABLE_NAME='accepted_user_events' THEN source:=NEW.id;
 ELSIF TG_TABLE_NAME='outbox_events' THEN source:=NEW.source_ref;
 ELSE SELECT source_ref INTO source FROM banking.outbox_events WHERE id=NEW.event_id;
 END IF;
 IF EXISTS(SELECT 1 FROM banking.erasure_ledger WHERE source_id=source) THEN
  IF TG_TABLE_NAME='accepted_user_events' AND NEW.payload='{}'::jsonb THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME='preference_events' AND NEW.operation='DELETE' AND NEW.normalized_value IS NULL AND NEW.source_span IS NULL THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME='preference_proposals' AND NEW.proposals='[]'::jsonb AND NEW.failure_code='source_deleted' THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME='memory_lineage' AND NEW.cleanup_status='deleted' AND NEW.suppressed_at IS NOT NULL THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME='outbox_events' AND NEW.status='dead_letter' AND NEW.failure_code='source_deleted' AND NEW.minimal_payload='{}'::jsonb AND NEW.trace_context='{}'::jsonb THEN RETURN NEW; END IF;
  RAISE EXCEPTION 'Source erased' USING ERRCODE='55000';
 END IF;
 RETURN NEW;
END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['accepted_user_events','preference_events','preference_proposals','memory_lineage','outbox_events','consumer_checkpoints','consumer_terminal_events'] LOOP
 EXECUTE format('CREATE TRIGGER blocked_source BEFORE INSERT OR UPDATE ON banking.%I FOR EACH ROW EXECUTE FUNCTION banking.reject_erased_work()',t);
 END LOOP;
END $$;
CREATE FUNCTION banking.restore_memory_check(p_source uuid) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE customer uuid:=nullif(current_setting('bank.customer_id',true),'')::uuid; snapshot jsonb; n int; remaining int;
BEGIN
 SELECT b.snapshot INTO snapshot FROM banking.quarantined_memory_backups b
 JOIN banking.erasure_ledger e USING(source_id,customer_id) WHERE b.source_id=p_source AND b.customer_id=customer;
 IF NOT FOUND THEN RAISE EXCEPTION 'Missing erased backup' USING ERRCODE='P0002'; END IF;
 -- Restore only into a private, transaction-local quarantine. The independent
 -- erasure ledger was not part of the snapshot and is consulted before promotion.
 CREATE TEMP TABLE t024_restore_quarantine(source_id uuid,content jsonb) ON COMMIT DROP;
 INSERT INTO t024_restore_quarantine VALUES(p_source,snapshot->'source');
 INSERT INTO t024_restore_quarantine SELECT p_source,value FROM jsonb_array_elements(snapshot->'derivatives');
 SELECT count(*) INTO n FROM t024_restore_quarantine;
 UPDATE t024_restore_quarantine q SET content='{}' WHERE EXISTS(SELECT 1 FROM banking.erasure_ledger e WHERE e.source_id=q.source_id AND e.customer_id=customer);
 SELECT count(*) INTO remaining FROM t024_restore_quarantine WHERE content<>'{}'::jsonb;
 IF remaining<>0 THEN RAISE EXCEPTION 'Quarantined restore refused'; END IF;
 RETURN jsonb_build_object('source_id',p_source,'restore_status','scrubbed_in_isolated_quarantine',
 'restored_rows',n,'scrubbed_rows',n,'remaining_content_rows',remaining,'ledger_outside_snapshot',true,
 'serving_enabled',false,'backup_status','retained_quarantined','production_backup_deletion_claim',false);
END $$;
REVOKE ALL ON FUNCTION banking.write_context(text,uuid,bigint,jsonb),banking.create_memory(uuid,uuid,text),
 banking.derive_memory(uuid[],text,uuid,text),banking.read_memories(uuid,text,uuid),banking.erase_memory_source(uuid),banking.reject_erased_work(),banking.restore_memory_check(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.write_context(text,uuid,bigint,jsonb),banking.create_memory(uuid,uuid,text),
 banking.derive_memory(uuid[],text,uuid,text),banking.read_memories(uuid,text,uuid),banking.erase_memory_source(uuid),banking.restore_memory_check(uuid) TO @RUNTIME@;
