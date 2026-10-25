"""Guarded T05.3 fixture world; no ordinary-demo status advance."""
import psycopg
from evals.confirmation_world import ConfirmationWorld
from evals.banking_world import database_dsn
from store.banking_migrate import apply

class ContinuityWorld(ConfirmationWorld):
    def reset(self,dsn=None):
        super().reset(dsn)
        with psycopg.connect(database_dsn(self.config['dsns']['migrator'],self.name)) as c:
            apply(c,self.config['roles'],through='0032')
        return self.semantic_hash()
