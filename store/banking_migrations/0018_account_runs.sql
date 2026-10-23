-- Internal bounded enquiry runs. No public turn acceptance or transport.
CREATE TABLE banking.account_runs (
 id uuid PRIMARY KEY, customer_id uuid NOT NULL REFERENCES banking.customers(customer_id),
 session_binding text NOT NULL, fingerprint text NOT NULL,
 started_at timestamptz NOT NULL DEFAULT clock_timestamp(), deadline timestamptz NOT NULL,
 generation bigint NOT NULL DEFAULT 0, model_calls integer NOT NULL DEFAULT 0,
 tool_calls integer NOT NULL DEFAULT 0, repairs integer NOT NULL DEFAULT 0,
 result jsonb, CHECK(deadline>started_at)
);
CREATE TABLE banking.account_attempts (
 run_id uuid NOT NULL REFERENCES banking.account_runs(id), attempt integer NOT NULL,
 customer_id uuid NOT NULL, generation bigint NOT NULL, kind text NOT NULL,
 reserved_at timestamptz NOT NULL DEFAULT clock_timestamp(), usage jsonb,
 PRIMARY KEY(run_id,attempt)
);
ALTER TABLE banking.account_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.account_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE banking.account_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.account_attempts FORCE ROW LEVEL SECURITY;
CREATE POLICY run_owner ON banking.account_runs USING(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid);
CREATE POLICY attempt_owner ON banking.account_attempts USING(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid);
REVOKE ALL ON banking.account_runs,banking.account_attempts FROM PUBLIC,@RUNTIME@,@WORKER@,@AUTH@,@DISPATCHER@;
GRANT SELECT ON banking.account_runs,banking.account_attempts TO @RUNTIME@,@WORKER@;
CREATE FUNCTION banking.claim_account_run(p_id uuid,p_binding text,p_fingerprint text,p_seconds double precision)
RETURNS banking.account_runs LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE r banking.account_runs; owner_id uuid:=nullif(current_setting('bank.customer_id',true),'')::uuid;
BEGIN
 IF owner_id IS NULL OR length(p_binding)<>64 OR length(p_fingerprint)<>64 OR NOT(p_seconds>0 AND p_seconds<=30) THEN RAISE EXCEPTION 'invalid run'; END IF;
 INSERT INTO banking.account_runs(id,customer_id,session_binding,fingerprint,deadline)
 VALUES(p_id,owner_id,p_binding,p_fingerprint,clock_timestamp()+p_seconds*interval '1 second') ON CONFLICT DO NOTHING;
 SELECT * INTO r FROM banking.account_runs WHERE id=p_id FOR UPDATE;
 IF r.id IS NULL OR r.session_binding<>p_binding OR r.fingerprint<>p_fingerprint THEN RAISE EXCEPTION 'run binding conflict' USING ERRCODE='23505'; END IF;
 IF r.result IS NULL THEN UPDATE banking.account_runs SET generation=generation+1 WHERE id=p_id RETURNING * INTO r; END IF;
 RETURN r;
END $$;
CREATE FUNCTION banking.reserve_account_attempt(p_id uuid,p_binding text,p_generation bigint,p_kind text)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
DECLARE r banking.account_runs; n integer;
BEGIN
 SELECT * INTO r FROM banking.account_runs WHERE id=p_id AND session_binding=p_binding FOR UPDATE;
 IF r.id IS NULL OR r.generation<>p_generation OR r.result IS NOT NULL OR r.deadline<=clock_timestamp() THEN RETURN NULL; END IF;
 IF p_kind NOT IN ('model','repair','tool') OR r.model_calls+r.tool_calls>=6 OR
 (p_kind IN ('model','repair') AND r.model_calls>=3) OR (p_kind='repair' AND r.repairs>=1) OR (p_kind='tool' AND r.tool_calls>=2) THEN RETURN NULL; END IF;
 n:=r.model_calls+r.tool_calls+1;
 UPDATE banking.account_runs SET model_calls=model_calls+CASE WHEN p_kind<>'tool' THEN 1 ELSE 0 END,
 tool_calls=tool_calls+CASE WHEN p_kind='tool' THEN 1 ELSE 0 END, repairs=repairs+CASE WHEN p_kind='repair' THEN 1 ELSE 0 END WHERE id=p_id;
 INSERT INTO banking.account_attempts(run_id,attempt,customer_id,generation,kind) VALUES(p_id,n,r.customer_id,p_generation,p_kind);
 RETURN n;
END $$;
CREATE FUNCTION banking.record_account_usage(p_id uuid,p_attempt integer,p_usage jsonb) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
 UPDATE banking.account_attempts SET usage=p_usage WHERE run_id=p_id AND attempt=p_attempt AND usage IS NULL;
$$;
CREATE FUNCTION banking.publish_account_run(p_id uuid,p_binding text,p_generation bigint,p_result jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,banking AS $$
BEGIN
 UPDATE banking.account_runs SET result=p_result WHERE id=p_id AND session_binding=p_binding AND generation=p_generation AND result IS NULL AND deadline>clock_timestamp();
 RETURN FOUND;
END $$;
REVOKE ALL ON FUNCTION banking.claim_account_run(uuid,text,text,double precision),banking.reserve_account_attempt(uuid,text,bigint,text),banking.record_account_usage(uuid,integer,jsonb),banking.publish_account_run(uuid,text,bigint,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.claim_account_run(uuid,text,text,double precision),banking.reserve_account_attempt(uuid,text,bigint,text),banking.record_account_usage(uuid,integer,jsonb),banking.publish_account_run(uuid,text,bigint,jsonb) TO @RUNTIME@,@WORKER@;
