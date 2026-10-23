-- SPEC07 / INV02 / INV07 / INV08: synthetic read-only card authority.
CREATE TABLE banking.cards (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 customer_id uuid NOT NULL REFERENCES banking.customers(customer_id),
 display_name text NOT NULL CHECK(length(display_name) BETWEEN 1 AND 100),
 last_four text NOT NULL CHECK(last_four ~ '^[0-9]{4}$'),
 kind text NOT NULL CHECK(kind IN ('debit','credit')),
 status text NOT NULL CHECK(status IN ('active','blocked','expired','closed')),
 currency text NOT NULL CHECK(currency IN ('INR','USD','EUR','GBP','JPY','KWD')),
 daily_limit_minor bigint NOT NULL CHECK(daily_limit_minor>=0),
 credit_limit_minor bigint CHECK(credit_limit_minor>=0),
 available_credit_minor bigint,
 outstanding_minor bigint,
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 as_of timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(id,customer_id),
 CHECK ((kind='debit' AND credit_limit_minor IS NULL AND available_credit_minor IS NULL AND outstanding_minor IS NULL)
     OR (kind='credit' AND credit_limit_minor IS NOT NULL AND available_credit_minor IS NOT NULL AND outstanding_minor IS NOT NULL))
);
CREATE TABLE banking.card_transactions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 card_id uuid NOT NULL,
 customer_id uuid NOT NULL,
 amount_minor bigint NOT NULL,
 status text NOT NULL CHECK(status IN ('declined','pending','posted','reversed')),
 occurred_at timestamptz NOT NULL,
 reason text CHECK(reason IS NULL OR length(reason)<=500),
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 as_of timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(card_id,customer_id) REFERENCES banking.cards(id,customer_id)
);
CREATE INDEX card_history_order ON banking.card_transactions(card_id,occurred_at,id);
CREATE INDEX card_suffix_lookup ON banking.cards(customer_id,last_four,id);
CREATE TRIGGER card_revision BEFORE UPDATE ON banking.cards FOR EACH ROW EXECUTE FUNCTION banking.account_revision();
CREATE TRIGGER card_transaction_revision BEFORE UPDATE ON banking.card_transactions FOR EACH ROW EXECUTE FUNCTION banking.account_revision();
CREATE FUNCTION banking.card_history_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP<>'INSERT' THEN UPDATE banking.cards SET version=version+1 WHERE id=OLD.card_id AND customer_id=OLD.customer_id; END IF;
 IF TG_OP='INSERT' OR (TG_OP='UPDATE' AND (NEW.card_id,NEW.customer_id) IS DISTINCT FROM (OLD.card_id,OLD.customer_id)) THEN
  UPDATE banking.cards SET version=version+1 WHERE id=NEW.card_id AND customer_id=NEW.customer_id;
 END IF;
 RETURN NULL;
END $$;
CREATE TRIGGER card_history_revision AFTER INSERT OR UPDATE OR DELETE ON banking.card_transactions FOR EACH ROW EXECUTE FUNCTION banking.card_history_revision();
ALTER TABLE banking.cards ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.cards FORCE ROW LEVEL SECURITY;
ALTER TABLE banking.card_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.card_transactions FORCE ROW LEVEL SECURITY;
CREATE POLICY card_owner ON banking.cards USING(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid)
 WITH CHECK(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid);
CREATE POLICY card_transaction_owner ON banking.card_transactions USING(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid)
 WITH CHECK(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid);
REVOKE ALL ON banking.cards,banking.card_transactions FROM PUBLIC,@RUNTIME@,@AUTH@,@WORKER@,@DISPATCHER@;
GRANT SELECT ON banking.cards,banking.card_transactions TO @RUNTIME@,@WORKER@;
