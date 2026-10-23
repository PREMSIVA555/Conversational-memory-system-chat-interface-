"""T04.2 fixtures extend guarded private worlds, never the normal demo bank."""
import json
import psycopg
from evals.card_world import CardWorld
from evals.banking_world import ROOT, database_dsn
from store.banking_migrate import apply


class LoanWorld(CardWorld):
    @property
    def fixture(self):return json.loads((ROOT/'evals/fixtures/p04_loan_slice.json').read_text(encoding='utf-8'))

    def reset(self,dsn=None):
        super().reset(dsn)
        with psycopg.connect(database_dsn(self.config['dsns']['migrator'],self.name)) as c:apply(c,self.config['roles'],through='0026')
        f=self.fixture
        with psycopg.connect(self.admin_dsn) as c:
            for l in f['loans']:
                c.execute('''INSERT INTO banking.loans(id,customer_id,display_name,status,currency,principal_minor,apr_percent,monthly_payment_minor,as_of)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(l['id'],f['customers'][l['owner']],l['name'],l['status'],l['currency'],l['principal'],l['apr'],l['monthly'],f['domain_time']))
            for s in f['emi_schedule']:
                l=f['loans'][s['loan']]
                c.execute('''INSERT INTO banking.emi_schedule(id,loan_id,customer_id,installment_number,due_on,scheduled_minor,as_of)
                    VALUES(%s,%s,%s,%s,%s,%s,%s)''',(s['id'],l['id'],f['customers'][l['owner']],s['number'],s['due_on'],s['amount'],f['domain_time']))
            for p in f['loan_payments']:
                l=f['loans'][p['loan']];s=f['emi_schedule'][p['installment']]['id'] if p['installment'] is not None else None
                c.execute('''INSERT INTO banking.loan_payments(id,loan_id,customer_id,installment_id,amount_minor,status,occurred_at,reason,as_of)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(p['id'],l['id'],f['customers'][l['owner']],s,p['amount'],p['status'],p['occurred_at'],p['reason'],f['domain_time']))
            c.execute('ALTER TABLE banking.loans DISABLE TRIGGER loan_revision')
            c.execute('UPDATE banking.loans SET version=1,as_of=%s',(f['domain_time'],))
            c.execute('ALTER TABLE banking.loans ENABLE TRIGGER loan_revision')
        return self.semantic_hash()

    def oracle(self):
        result=super().oracle()
        with psycopg.connect(self.admin_dsn) as c:
            if c.execute("SELECT to_regclass('banking.loans')").fetchone()[0] is None:return dict(**result,loans=[],emi_schedule=[],loan_payments=[])
            result['loans']=c.execute('''SELECT id::text,customer_id::text,display_name,status,currency,principal_minor::text,
                apr_percent::text,monthly_payment_minor::text,version::text,as_of::text FROM banking.loans ORDER BY id''').fetchall()
            result['emi_schedule']=c.execute('''SELECT id::text,loan_id::text,customer_id::text,installment_number,due_on::text,
                scheduled_minor::text,version::text,as_of::text FROM banking.emi_schedule ORDER BY due_on,id''').fetchall()
            result['loan_payments']=c.execute('''SELECT id::text,loan_id::text,customer_id::text,installment_id::text,amount_minor::text,
                status,occurred_at::text,reason,version::text,as_of::text FROM banking.loan_payments ORDER BY occurred_at,id''').fetchall()
        return result
