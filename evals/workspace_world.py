"""UX01 uses the approved guarded trial-world lifecycle, no P05 dependency."""
import psycopg,json
from pathlib import Path
from evals.policy_world import PolicyWorld
from evals.banking_world import database_dsn
from store.banking_migrate import apply

class WorkspaceWorld(PolicyWorld):
    def reset(self,dsn=None):
        super().reset(dsn)
        with psycopg.connect(database_dsn(self.config['dsns']['migrator'],self.name)) as conn:apply(conn,self.config['roles'],through='0029')
        # This guarded new trial database has no legacy writer/store. Enable its
        # new preference path as fixture setup, without inventing a cutover attestation.
        self.guard()
        with psycopg.connect(self.admin_dsn) as conn:conn.execute('UPDATE banking.memory_rollout SET new_writes_enabled=true')
        from store.banking_policies import stage,activate
        manifest=json.loads((Path(__file__).resolve().parents[1]/'evals/fixtures/p04_policy_queries.json').read_text(encoding='utf-8'))['manifest']
        with psycopg.connect(database_dsn(self.config['dsns']['migrator'],self.name),autocommit=True) as conn:
            sid=stage(conn,manifest);activate(conn,sid,expected=None)
        return self.semantic_hash()
