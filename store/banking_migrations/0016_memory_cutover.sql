-- T02.5 additive rollout control; rollback never restores the legacy writer.
CREATE TABLE banking.memory_rollout (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 new_writes_enabled boolean NOT NULL DEFAULT false,
 legacy_guard_attestation jsonb,
 generation bigint NOT NULL DEFAULT 1 CHECK(generation>0),
 changed_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE banking.memory_rollout ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.memory_rollout FORCE ROW LEVEL SECURITY;
CREATE POLICY read_rollout ON banking.memory_rollout FOR SELECT USING(true);
CREATE POLICY control_rollout ON banking.memory_rollout FOR ALL TO @MIGRATOR@ USING(true) WITH CHECK(true);
INSERT INTO banking.memory_rollout(singleton) VALUES(true);
GRANT SELECT ON banking.memory_rollout TO @RUNTIME@,@WORKER@;
CREATE FUNCTION banking.set_memory_rollout(p_enabled boolean,p_expected bigint) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE r banking.memory_rollout;
BEGIN
 SELECT * INTO STRICT r FROM banking.memory_rollout WHERE singleton FOR UPDATE;
 IF r.generation<>p_expected THEN RAISE EXCEPTION 'Stale rollout version' USING ERRCODE='40001'; END IF;
 IF p_enabled AND r.legacy_guard_attestation IS NULL THEN
 RAISE EXCEPTION 'Legacy database guard installation required' USING ERRCODE='55000'; END IF;
 IF r.new_writes_enabled IS DISTINCT FROM p_enabled THEN
 UPDATE banking.memory_rollout SET new_writes_enabled=p_enabled,generation=generation+1,changed_at=clock_timestamp()
 WHERE singleton RETURNING * INTO r;
 END IF;
 RETURN jsonb_build_object('new_writes_enabled',r.new_writes_enabled,'generation',r.generation,
 'legacy_preference_writer','python_guarded','legacy_database_guard',r.legacy_guard_attestation,
 'rollback','preserve_events_and_tombstones');
END $$;
REVOKE ALL ON FUNCTION banking.set_memory_rollout(boolean,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.set_memory_rollout(boolean,bigint) TO @MIGRATOR@;
CREATE FUNCTION banking.record_legacy_guard(p_identity jsonb) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
BEGIN
 IF (length(p_identity->>'database')>0 AND p_identity->>'system_identifier' ~ '^[0-9]+$' AND
 p_identity->>'guard_sha256' ~ '^[0-9a-f]{64}$' AND p_identity->>'snapshot_sha256' ~ '^[0-9a-f]{64}$' AND
 p_identity->>'status'='installed') IS NOT TRUE THEN
 RAISE EXCEPTION 'Invalid legacy guard installation attestation' USING ERRCODE='22023'; END IF;
 UPDATE banking.memory_rollout SET legacy_guard_attestation=p_identity,generation=generation+1,changed_at=clock_timestamp() WHERE singleton;
END $$;
REVOKE ALL ON FUNCTION banking.record_legacy_guard(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.record_legacy_guard(jsonb) TO @MIGRATOR@;

ALTER FUNCTION banking.accept_preference(uuid,text,uuid,jsonb,text,jsonb) RENAME TO accept_preference_pre_cutover;
REVOKE ALL ON FUNCTION banking.accept_preference_pre_cutover(uuid,text,uuid,jsonb,text,jsonb) FROM @RUNTIME@;
CREATE FUNCTION banking.accept_preference(p_client uuid,p_type text,p_conversation uuid,p_payload jsonb,p_session text,p_proposals jsonb)
 RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE enabled boolean;
BEGIN
 -- Share lock persists through commit: disabling waits for earlier acceptances.
 SELECT new_writes_enabled INTO STRICT enabled FROM banking.memory_rollout WHERE singleton FOR SHARE;
 IF NOT enabled THEN RAISE EXCEPTION 'Preference writes disabled' USING ERRCODE='55000'; END IF;
 RETURN banking.accept_preference_pre_cutover(p_client,p_type,p_conversation,p_payload,p_session,p_proposals);
END $$;
REVOKE ALL ON FUNCTION banking.accept_preference(uuid,text,uuid,jsonb,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.accept_preference(uuid,text,uuid,jsonb,text,jsonb) TO @RUNTIME@;
CREATE FUNCTION banking.guard_preference_acceptance() RETURNS trigger
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE enabled boolean;
BEGIN
 IF NEW.payload->>'handler'='preferences.v1' OR NEW.payload->>'key' IN ('preferred_response_language','preferred_response_format') THEN
 SELECT new_writes_enabled INTO STRICT enabled FROM banking.memory_rollout WHERE singleton FOR SHARE;
 IF NOT enabled THEN RAISE EXCEPTION 'Preference writes disabled' USING ERRCODE='55000'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER cutover_preference_acceptance BEFORE INSERT ON banking.accepted_user_events
 FOR EACH ROW EXECUTE FUNCTION banking.guard_preference_acceptance();

-- No mapping is inferred from legacy subject IDs, text or timestamps.
CREATE TABLE banking.legacy_owner_links (
 legacy_subject_id uuid PRIMARY KEY, customer_id uuid NOT NULL REFERENCES banking.customers,
 evidence_ref text NOT NULL CHECK(length(evidence_ref) BETWEEN 1 AND 300)
);
CREATE TABLE banking.legacy_backfill_receipts (
 legacy_id uuid PRIMARY KEY, customer_id uuid NOT NULL, source_id uuid NOT NULL,
 snapshot_sha256 text NOT NULL CHECK(snapshot_sha256 ~ '^[0-9a-f]{64}$'),
 reviewed_plan_sha256 text NOT NULL CHECK(reviewed_plan_sha256 ~ '^[0-9a-f]{64}$'),
 outcome text NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(source_id,customer_id) REFERENCES banking.accepted_user_events(id,customer_id)
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['legacy_owner_links','legacy_backfill_receipts'] LOOP
 EXECUTE format('ALTER TABLE banking.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE banking.%I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('CREATE POLICY owner_context ON banking.%I USING(customer_id=nullif(current_setting(''bank.customer_id'',true),'''')::uuid) WITH CHECK(customer_id=nullif(current_setting(''bank.customer_id'',true),'''')::uuid)',t);
 END LOOP;
END $$;
CREATE FUNCTION banking.backfill_legacy_preference(p_legacy uuid,p_subject uuid,p_source uuid,p_key text,p_value text,p_snapshot text,p_review text)
 RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE customer uuid:=nullif(current_setting('bank.customer_id',true),'')::uuid;
 s banking.accepted_user_events; prior banking.legacy_backfill_receipts; outcome text;
BEGIN
 PERFORM 1 FROM banking.customers WHERE customer_id=customer FOR UPDATE;
 IF NOT EXISTS(SELECT 1 FROM banking.legacy_owner_links WHERE legacy_subject_id=p_subject AND customer_id=customer)
 THEN RAISE EXCEPTION 'Unreviewed legacy owner' USING ERRCODE='42501'; END IF;
 SELECT * INTO STRICT s FROM banking.accepted_user_events WHERE id=p_source AND customer_id=customer AND actor_ref='user';
 IF EXISTS(SELECT 1 FROM banking.erasure_ledger WHERE source_id=p_source) THEN
 RAISE EXCEPTION 'Source erased' USING ERRCODE='55000'; END IF;
 IF NOT EXISTS(SELECT 1 FROM banking.preference_proposals WHERE source_id=p_source AND
 proposals @> jsonb_build_array(jsonb_build_object('key',p_key,'value',p_value,'operation','SET','scope_key','global')))
 THEN RAISE EXCEPTION 'Missing accepted typed source' USING ERRCODE='22023'; END IF;
 SELECT * INTO prior FROM banking.legacy_backfill_receipts WHERE legacy_id=p_legacy;
 IF FOUND THEN
 IF prior.source_id<>p_source OR prior.snapshot_sha256<>p_snapshot OR prior.reviewed_plan_sha256<>p_review THEN
 RAISE EXCEPTION 'Backfill replay conflict' USING ERRCODE='23505'; END IF;
 RETURN 'duplicate'; END IF;
 outcome:=banking.project_preference_internal(p_source,p_key,'global',NULL,'SET',to_jsonb(p_value));
 INSERT INTO banking.legacy_backfill_receipts(legacy_id,customer_id,source_id,snapshot_sha256,reviewed_plan_sha256,outcome)
 VALUES(p_legacy,customer,p_source,p_snapshot,p_review,outcome);
 RETURN outcome;
END $$;
REVOKE ALL ON FUNCTION banking.backfill_legacy_preference(uuid,uuid,uuid,text,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.backfill_legacy_preference(uuid,uuid,uuid,text,text,text,text) TO @MIGRATOR@;
