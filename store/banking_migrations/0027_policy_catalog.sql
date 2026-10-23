-- Synthetic public policies only. Customer data remains in forced-RLS tables.
CREATE TABLE banking.policy_sets (
 id text PRIMARY KEY CHECK(id ~ '^[a-f0-9]{64}$'),
 manifest jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE banking.policy_active (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 set_id text NOT NULL REFERENCES banking.policy_sets(id)
);
CREATE FUNCTION banking.policy_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Policy sets are immutable'; END $$;
CREATE TRIGGER policy_immutable BEFORE UPDATE OR DELETE ON banking.policy_sets
 FOR EACH ROW EXECUTE FUNCTION banking.policy_immutable();
REVOKE ALL ON banking.policy_sets,banking.policy_active FROM PUBLIC,@RUNTIME@,@AUTH@,@WORKER@,@DISPATCHER@;
GRANT SELECT ON banking.policy_sets,banking.policy_active TO @RUNTIME@,@WORKER@;
CREATE FUNCTION banking.policy_lexical(body text, question text) RETURNS real
 LANGUAGE sql IMMUTABLE STRICT AS $$
 SELECT ts_rank_cd(to_tsvector('simple',body),plainto_tsquery('simple',question))
$$;
REVOKE ALL ON FUNCTION banking.policy_lexical(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION banking.policy_lexical(text,text) TO @RUNTIME@,@WORKER@;
