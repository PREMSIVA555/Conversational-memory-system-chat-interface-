"""T04.1 synthetic cards layered onto the existing manifest-owned trial world."""
import json
from pathlib import Path
import psycopg
from evals.banking_world import BankingWorld, ROOT, database_dsn
from store.banking_migrate import apply

FIXTURE=ROOT/'evals/fixtures/p04_card_slice.json'

class CardWorld(BankingWorld):
    @property
    def fixture(self):return json.loads(FIXTURE.read_text(encoding='utf-8'))

    def reset(self,dsn=None):
        super().reset(dsn)
        with psycopg.connect(database_dsn(self.config['dsns']['migrator'],self.name)) as c:
            apply(c,self.config['roles'],through='0025')
        f=self.fixture
        with psycopg.connect(self.admin_dsn) as c:
            for card in f['cards']:
                c.execute('''INSERT INTO banking.cards(id,customer_id,display_name,last_four,kind,status,currency,
                    daily_limit_minor,credit_limit_minor,available_credit_minor,outstanding_minor,as_of)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (card['id'],f['customers'][card['owner']],card['name'],card['last_four'],card['kind'],card['status'],card['currency'],
                     card['daily'],card['credit'],card['available'],card['outstanding'],f['domain_time']))
            for t in f['card_transactions']:
                card=f['cards'][t['card']]
                c.execute('''INSERT INTO banking.card_transactions(id,card_id,customer_id,amount_minor,status,occurred_at,reason,as_of)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (t['id'],card['id'],f['customers'][card['owner']],t['amount'],t['status'],t['occurred_at'],t['reason'],f['domain_time']))
            c.execute('ALTER TABLE banking.cards DISABLE TRIGGER card_revision')
            c.execute('UPDATE banking.cards SET version=1,as_of=%s',(f['domain_time'],))
            c.execute('ALTER TABLE banking.cards ENABLE TRIGGER card_revision')
        return self.semantic_hash()

    def oracle(self):
        result=super().oracle()
        with psycopg.connect(self.admin_dsn) as c:
            if c.execute("SELECT to_regclass('banking.cards')").fetchone()[0] is None:
                return dict(**result,cards=[],card_transactions=[])
            result['cards']=c.execute('''SELECT id::text,customer_id::text,display_name,last_four,kind,status,currency,
                daily_limit_minor::text,credit_limit_minor::text,available_credit_minor::text,outstanding_minor::text,
                version::text,as_of::text FROM banking.cards ORDER BY id''').fetchall()
            result['card_transactions']=c.execute('''SELECT id::text,card_id::text,customer_id::text,amount_minor::text,status,
                occurred_at::text,reason,version::text,as_of::text FROM banking.card_transactions ORDER BY occurred_at,id''').fetchall()
        return result
