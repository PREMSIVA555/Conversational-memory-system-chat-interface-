"""Private, manifest-guarded PostgreSQL/Redis support test world."""
import psycopg
from evals.workspace_world import WorkspaceWorld
from evals.banking_world import database_dsn
from store.banking_migrate import apply


class SupportWorld(WorkspaceWorld):
    def reset(self, dsn=None):
        super().reset(dsn)
        # WorkspaceWorld installs the exact preserved0028 and approved0029 in
        # order, including preference fixtures; the ordinary demo is untouched.
        with psycopg.connect(database_dsn(self.config['dsns']['migrator'], self.name)) as conn:
            apply(conn, self.config['roles'], through='0030')
        return self.semantic_hash()
