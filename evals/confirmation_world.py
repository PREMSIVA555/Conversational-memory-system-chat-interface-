"""Private T05.2 worlds preserve the disabled T05.1 verification baseline."""
import psycopg
from evals.support_world import SupportWorld
from evals.banking_world import database_dsn
from store.banking_migrate import apply

class ConfirmationWorld(SupportWorld):
    def reset(self,dsn=None):
        super().reset(dsn)
        with psycopg.connect(database_dsn(self.config['dsns']['migrator'],self.name)) as c:
            apply(c,self.config['roles'],through='0031')
        return self.semantic_hash()
