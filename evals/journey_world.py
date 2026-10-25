"""T05.4 private fixture: original declined-payment example, never ordinary data."""
from evals.continuity_world import ContinuityWorld
from evals.banking_world import database_dsn
from scripts.p054_policy import with_card_support
from store.banking_policies import stage,activate
import psycopg

class JourneyWorld(ContinuityWorld):
    def reset(self,dsn=None):
        super().reset(dsn)
        with psycopg.connect(database_dsn(self.config['dsns']['migrator'],self.name),autocommit=True) as c:
            old,manifest=c.execute('SELECT s.id,s.manifest FROM banking.policy_active a JOIN banking.policy_sets s ON s.id=a.set_id WHERE a.singleton').fetchone()
            new=stage(c,with_card_support(manifest));activate(c,new,expected=old)
        return self.semantic_hash()
    @property
    def fixture(self):
        f=super().fixture
        f['cards'][1].update(name='Synthetic Visa',last_four='4112')
        f['cards'][0].update(name='Synthetic Mastercard',last_four='7220')
        f['card_transactions'][0].update(amount='200000',reason='merchant restriction')
        return f
