-- SPEC07 / INV06 / INV07 / INV08: schedule is distinct from actual payments.
CREATE TABLE banking.loans (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 customer_id uuid NOT NULL REFERENCES banking.customers(customer_id),
 display_name text NOT NULL CHECK(length(display_name) BETWEEN 1 AND 100),
 status text NOT NULL CHECK(status IN ('active','closed')),
 currency text NOT NULL CHECK(currency IN ('INR','USD','EUR','GBP','JPY','KWD')),
 principal_minor bigint NOT NULL CHECK(principal_minor>=0),
 apr_percent numeric NOT NULL CHECK(apr_percent>=0 AND apr_percent<=100 AND scale(apr_percent)<=8),
 monthly_payment_minor bigint NOT NULL CHECK(monthly_payment_minor>=0),
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 as_of timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(id,customer_id)
);
CREATE TABLE banking.emi_schedule (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 loan_id uuid NOT NULL,
 customer_id uuid NOT NULL,
 installment_number integer NOT NULL CHECK(installment_number>0),
 due_on date NOT NULL,
 scheduled_minor bigint NOT NULL CHECK(scheduled_minor>=0),
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 as_of timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(loan_id,installment_number),
 UNIQUE(id,loan_id,customer_id),
 FOREIGN KEY(loan_id,customer_id) REFERENCES banking.loans(id,customer_id)
);
CREATE TABLE banking.loan_payments (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 loan_id uuid NOT NULL,
 customer_id uuid NOT NULL,
 installment_id uuid,
 amount_minor bigint NOT NULL CHECK(amount_minor>=0),
 status text NOT NULL CHECK(status IN ('declined','pending','posted','reversed')),
 occurred_at timestamptz NOT NULL,
 reason text CHECK(reason IS NULL OR length(reason)<=500),
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 as_of timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(loan_id,customer_id) REFERENCES banking.loans(id,customer_id),
 FOREIGN KEY(installment_id,loan_id,customer_id) REFERENCES banking.emi_schedule(id,loan_id,customer_id)
);
CREATE INDEX emi_due_order ON banking.emi_schedule(loan_id,due_on,id);
CREATE INDEX loan_payment_order ON banking.loan_payments(loan_id,occurred_at,id);
CREATE TRIGGER loan_revision BEFORE UPDATE ON banking.loans FOR EACH ROW EXECUTE FUNCTION banking.account_revision();
CREATE TRIGGER installment_revision BEFORE UPDATE ON banking.emi_schedule FOR EACH ROW EXECUTE FUNCTION banking.account_revision();
CREATE TRIGGER loan_payment_revision BEFORE UPDATE ON banking.loan_payments FOR EACH ROW EXECUTE FUNCTION banking.account_revision();
CREATE FUNCTION banking.loan_evidence_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP<>'INSERT' THEN UPDATE banking.loans SET version=version+1 WHERE id=OLD.loan_id AND customer_id=OLD.customer_id; END IF;
 IF TG_OP='INSERT' OR (TG_OP='UPDATE' AND (NEW.loan_id,NEW.customer_id) IS DISTINCT FROM (OLD.loan_id,OLD.customer_id)) THEN
  UPDATE banking.loans SET version=version+1 WHERE id=NEW.loan_id AND customer_id=NEW.customer_id;
 END IF;
 RETURN NULL;
END $$;
CREATE TRIGGER installment_loan_revision AFTER INSERT OR UPDATE OR DELETE ON banking.emi_schedule FOR EACH ROW EXECUTE FUNCTION banking.loan_evidence_revision();
CREATE TRIGGER payment_loan_revision AFTER INSERT OR UPDATE OR DELETE ON banking.loan_payments FOR EACH ROW EXECUTE FUNCTION banking.loan_evidence_revision();
ALTER TABLE banking.loans ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.loans FORCE ROW LEVEL SECURITY;
ALTER TABLE banking.emi_schedule ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.emi_schedule FORCE ROW LEVEL SECURITY;
ALTER TABLE banking.loan_payments ENABLE ROW LEVEL SECURITY;
ALTER TABLE banking.loan_payments FORCE ROW LEVEL SECURITY;
CREATE POLICY loan_owner ON banking.loans USING(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid) WITH CHECK(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid);
CREATE POLICY schedule_owner ON banking.emi_schedule USING(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid) WITH CHECK(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid);
CREATE POLICY loan_payment_owner ON banking.loan_payments USING(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid) WITH CHECK(customer_id=nullif(current_setting('bank.customer_id',true),'')::uuid);
REVOKE ALL ON banking.loans,banking.emi_schedule,banking.loan_payments FROM PUBLIC,@RUNTIME@,@AUTH@,@WORKER@,@DISPATCHER@;
GRANT SELECT ON banking.loans,banking.emi_schedule,banking.loan_payments TO @RUNTIME@,@WORKER@;
