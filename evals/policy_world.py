"""T04.3 uses the existing guarded private-world lifecycle."""
import psycopg
from evals.loan_world import LoanWorld
from evals.banking_world import database_dsn
from store.banking_migrate import apply


class PolicyWorld(LoanWorld):
    def reset(self, dsn=None):
        super().reset(dsn)
        with psycopg.connect(database_dsn(self.config['dsns']['migrator'], self.name)) as conn:
            apply(conn, self.config['roles'], through='0027')
        return self.semantic_hash()

    def oracle(self):
        result = super().oracle()
        with psycopg.connect(self.admin_dsn) as conn:
            if conn.execute("SELECT to_regclass('banking.policy_sets')").fetchone()[0]:
                result['policy_sets'] = conn.execute('SELECT id,manifest FROM banking.policy_sets ORDER BY id').fetchall()
                result['policy_active'] = conn.execute('SELECT set_id FROM banking.policy_active').fetchall()
        return result
