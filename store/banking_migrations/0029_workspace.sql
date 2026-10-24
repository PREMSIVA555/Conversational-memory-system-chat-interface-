-- UX01 is independent of pending P05 migration 0028.
CREATE TABLE banking.workspace_sessions (
 customer_id uuid NOT NULL REFERENCES banking.customers(customer_id), session_binding text NOT NULL,
 conversation_id uuid NOT NULL, PRIMARY KEY(customer_id,session_binding),
 FOREIGN KEY(conversation_id,customer_id) REFERENCES banking.conversations(id,customer_id)
);
CREATE TABLE banking.resource_context (
 customer_id uuid NOT NULL, session_binding text NOT NULL, conversation_id uuid NOT NULL,
 source_id uuid NOT NULL REFERENCES banking.accepted_user_events(id), sequence bigint NOT NULL,
 resource jsonb NOT NULL, refs jsonb NOT NULL CHECK(jsonb_array_length(refs)<=20),
 lineage uuid[] NOT NULL CHECK(cardinality(lineage)<=20),
 expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '15 minutes',
 PRIMARY KEY(customer_id,session_binding,conversation_id),
 FOREIGN KEY(conversation_id,customer_id) REFERENCES banking.conversations(id,customer_id)
);
ALTER TABLE banking.workspace_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.workspace_sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE banking.resource_context ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.resource_context FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant ON banking.workspace_sessions USING(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid);
CREATE POLICY tenant ON banking.resource_context USING(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid
 AND expires_at>clock_timestamp() AND NOT EXISTS(SELECT 1 FROM banking.erasure_ledger e WHERE e.source_id=ANY(lineage)));
CREATE POLICY context_maintenance ON banking.resource_context TO @MIGRATOR@ USING(true) WITH CHECK(true);
GRANT SELECT ON banking.workspace_sessions,banking.resource_context TO @RUNTIME@,@WORKER@;
CREATE FUNCTION banking.prepare_workspace(p_binding text,p_conversation uuid DEFAULT NULL)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE customer uuid:=nullif(current_setting('bank.customer_id',true),'')::uuid; cid uuid;
BEGIN
 PERFORM 1 FROM banking.customers WHERE customer_id=customer FOR UPDATE;
 IF NOT FOUND OR p_binding !~ '^[a-f0-9]{64}$' THEN RAISE EXCEPTION 'Invalid session' USING ERRCODE='42501';END IF;
 IF p_conversation IS NOT NULL THEN
  IF NOT EXISTS(SELECT 1 FROM banking.conversations WHERE id=p_conversation AND customer_id=customer) THEN RAISE EXCEPTION 'Not found' USING ERRCODE='P0002';END IF;
  cid:=p_conversation;
 ELSE
  SELECT conversation_id INTO cid FROM banking.workspace_sessions WHERE customer_id=customer AND session_binding=p_binding;
  IF cid IS NOT NULL THEN RETURN cid;END IF;
  INSERT INTO banking.conversations(customer_id) VALUES(customer) RETURNING id INTO cid;
  INSERT INTO banking.audit_events(customer_id,conversation_id,event) VALUES(customer,cid,'conversation.created');
 END IF;
 INSERT INTO banking.workspace_sessions VALUES(customer,p_binding,cid) ON CONFLICT(customer_id,session_binding)
 DO UPDATE SET conversation_id=EXCLUDED.conversation_id;
 RETURN cid;
END $$;
CREATE FUNCTION banking.finish_workspace_turn(p_id uuid,p_generation bigint,p_result jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE t banking.turns; s banking.accepted_user_events; parents uuid[]; ids uuid[]; ok boolean;
BEGIN
 SELECT * INTO t FROM banking.turns WHERE id=p_id FOR UPDATE;
 IF t.id IS NULL OR t.generation<>p_generation OR t.result IS NOT NULL OR t.lease_until<=clock_timestamp() THEN RETURN false;END IF;
 SELECT * INTO s FROM banking.accepted_user_events WHERE id=t.source_id;
 SELECT coalesce(array_agg(value::uuid),'{}') INTO parents FROM jsonb_array_elements_text(coalesce(p_result->'workspace'->'lineage','[]'));
 ids:=parents||ARRAY[t.source_id];
 IF cardinality(ids)>20 OR EXISTS(SELECT 1 FROM banking.erasure_ledger WHERE source_id=ANY(ids)) THEN
  p_result:='{"status":"unavailable","text":"Current bank information is unavailable. Please try again later.","evidence":[]}'::jsonb;
 END IF;
 ok:=banking.finish_turn(p_id,p_generation,p_result);
 IF ok AND p_result ? 'workspace' THEN
  INSERT INTO banking.resource_context(customer_id,session_binding,conversation_id,source_id,sequence,resource,refs,lineage)
  VALUES(t.customer_id,t.session_binding,t.conversation_id,t.source_id,s.sequence,coalesce(p_result->'workspace'->'resource','null'::jsonb),coalesce(p_result->'workspace'->'refs','[]'),ids)
  ON CONFLICT(customer_id,session_binding,conversation_id) DO UPDATE SET source_id=EXCLUDED.source_id,
   sequence=EXCLUDED.sequence,resource=EXCLUDED.resource,refs=EXCLUDED.refs,lineage=EXCLUDED.lineage,
   expires_at=clock_timestamp()+interval '15 minutes' WHERE banking.resource_context.sequence<EXCLUDED.sequence;
 END IF;
 RETURN ok;
END $$;
CREATE FUNCTION banking.erase_resource_context() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
BEGIN
 DELETE FROM banking.resource_context WHERE NEW.source_id=ANY(lineage);
 RETURN NEW;
END $$;
CREATE TRIGGER erase_resource_context AFTER INSERT ON banking.erasure_ledger FOR EACH ROW EXECUTE FUNCTION banking.erase_resource_context();
CREATE FUNCTION banking.clear_workspace(p_binding text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
BEGIN
 DELETE FROM banking.resource_context WHERE customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid
 AND (session_binding=p_binding OR expires_at<=clock_timestamp());
 DELETE FROM banking.workspace_sessions WHERE customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid AND session_binding=p_binding;
END $$;
REVOKE ALL ON FUNCTION banking.prepare_workspace(text,uuid),banking.finish_workspace_turn(uuid,bigint,jsonb),banking.erase_resource_context() FROM PUBLIC;
REVOKE ALL ON FUNCTION banking.clear_workspace(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.clear_workspace(text) TO @RUNTIME@;
GRANT EXECUTE ON FUNCTION banking.prepare_workspace(text,uuid) TO @RUNTIME@;
GRANT EXECUTE ON FUNCTION banking.finish_workspace_turn(uuid,bigint,jsonb) TO @WORKER@;
