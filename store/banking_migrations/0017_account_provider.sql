-- Synthetic enquiry authority. No live-bank import or legacy rewrite.
CREATE TABLE banking.accounts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 customer_id uuid NOT NULL REFERENCES banking.customers(customer_id),
 display_name text NOT NULL CHECK(length(display_name) BETWEEN 1 AND 100),
 currency text NOT NULL CHECK(currency IN ('INR','USD','EUR','GBP','JPY','KWD')),
 current_minor bigint NOT NULL,
 available_minor bigint NOT NULL,
 version bigint NOT NULL DEFAULT 1 CHECK(version > 0),
 as_of timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(id,customer_id)
);
CREATE TABLE banking.account_transactions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 account_id uuid NOT NULL,
 customer_id uuid NOT NULL,
 amount_minor bigint NOT NULL,
 status text NOT NULL CHECK(status IN ('declined','pending','posted','reversed')),
 booked_on date NOT NULL,
 reason text CHECK(reason IS NULL OR length(reason)<=500),
 version bigint NOT NULL DEFAULT 1 CHECK(version > 0),
 as_of timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(account_id,customer_id) REFERENCES banking.accounts(id,customer_id)
);
CREATE INDEX account_history_order ON banking.account_transactions(account_id,booked_on,id);
CREATE FUNCTION banking.account_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 NEW.version=OLD.version+1; NEW.as_of=clock_timestamp(); RETURN NEW;
END $$;
CREATE TRIGGER account_revision BEFORE UPDATE ON banking.accounts
 FOR EACH ROW EXECUTE FUNCTION banking.account_revision();
CREATE TRIGGER transaction_revision BEFORE UPDATE ON banking.account_transactions
 FOR EACH ROW EXECUTE FUNCTION banking.account_revision();
CREATE FUNCTION banking.history_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP<>'INSERT' THEN
  UPDATE banking.accounts SET version=version+1 WHERE id=OLD.account_id AND customer_id=OLD.customer_id;
 END IF;
 IF TG_OP='INSERT' OR (TG_OP='UPDATE' AND (NEW.account_id,NEW.customer_id) IS DISTINCT FROM (OLD.account_id,OLD.customer_id)) THEN
  UPDATE banking.accounts SET version=version+1 WHERE id=NEW.account_id AND customer_id=NEW.customer_id;
 END IF;
 RETURN NULL;
END $$;
CREATE TRIGGER history_revision AFTER INSERT OR UPDATE OR DELETE ON banking.account_transactions
 FOR EACH ROW EXECUTE FUNCTION banking.history_revision();
ALTER TABLE banking.accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.accounts FORCE ROW LEVEL SECURITY;
ALTER TABLE banking.account_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.account_transactions FORCE ROW LEVEL SECURITY;
CREATE POLICY account_owner ON banking.accounts USING(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid)
 WITH CHECK(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid);
CREATE POLICY transaction_owner ON banking.account_transactions USING(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid)
 WITH CHECK(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid);
REVOKE ALL ON banking.accounts,banking.account_transactions FROM PUBLIC,@RUNTIME@,@AUTH@,@WORKER@,@DISPATCHER@;
GRANT SELECT ON banking.accounts,banking.account_transactions TO @RUNTIME@,@WORKER@;
