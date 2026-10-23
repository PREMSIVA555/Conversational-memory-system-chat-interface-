"""P01 repositories accept only revalidated server contexts, never request IDs."""
from contextlib import asynccontextmanager
from uuid import UUID
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from store.db import ensure_selector_event_loop_policy

ensure_selector_event_loop_policy()


class Database:
    def __init__(self, settings, sessions):
        self.sessions = sessions
        kw = dict(min_size=0, max_size=4, open=False, timeout=3,
                  kwargs={'row_factory': dict_row}, check=AsyncConnectionPool.check_connection)
        self.runtime = AsyncConnectionPool(settings.runtime_dsn, **kw)
        self.identity = AsyncConnectionPool(settings.identity_dsn, **kw)
        self.worker = AsyncConnectionPool(settings.worker_dsn, **kw)

    async def open(self):
        await self.runtime.open()
        await self.identity.open()
        await self.worker.open()

    async def close(self):
        await self.runtime.close()
        await self.identity.close()
        await self.worker.close()

    async def inspect_role(self, pool):
        async with pool.connection() as c:
            row = await (await c.execute('''SELECT current_user AS role,
                rolsuper, rolbypassrls, rolcreatedb, rolcreaterole,
                EXISTS(SELECT 1 FROM pg_auth_members WHERE member=r.oid) AS has_membership,
                EXISTS(SELECT 1 FROM pg_class WHERE relowner=r.oid AND relnamespace=
                  (SELECT oid FROM pg_namespace WHERE nspname='banking')) AS owns_tables,
                EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='banking' AND nspowner=r.oid) AS owns_schema,
                (SELECT datdba=r.oid FROM pg_database WHERE datname=current_database()) AS owns_database
                FROM pg_roles r WHERE rolname=current_user''')).fetchone()
            if any(row[k] for k in row if k != 'role'):
                raise RuntimeError('Privileged banking connection refused')
            return row

    async def ready(self):
        await self.inspect_role(self.runtime)
        await self.inspect_role(self.identity)
        await self.inspect_role(self.worker)
        async with self.runtime.connection() as c:
            grants = await (await c.execute("""SELECT
              has_table_privilege(current_user,'banking.identity_links','SELECT') AS identity_read,
              has_table_privilege(current_user,'banking.audit_events','UPDATE,DELETE') AS audit_rewrite,
              has_schema_privilege(current_user,'banking','CREATE') AS schema_create,
              (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='banking' AND c.relname IN
                ('customers','conversations','conversation_scopes','audit_events')
                AND c.relrowsecurity AND c.relforcerowsecurity) AS protected_tables
              """)).fetchone()
            if grants['identity_read'] or grants['audit_rewrite'] or grants['schema_create'] or grants['protected_tables'] != 4:
                raise RuntimeError('Unsafe banking grants or RLS')
            row = await (await c.execute("SELECT count(*) AS n FROM banking.customers")).fetchone()
            if row['n'] != 0:
                raise RuntimeError('Missing tenant context did not deny rows')
        async with self.identity.connection() as c:
            grants=await (await c.execute("""SELECT
                has_table_privilege(current_user,'banking.identity_links','INSERT,UPDATE,DELETE') AS identity_write,
                has_table_privilege(current_user,'banking.conversations','SELECT') AS customer_read
                """)).fetchone()
            if any(grants.values()):
                raise RuntimeError('Unsafe identity role grants')
        return True

    async def map_identity(self, issuer, subject):
        async with self.identity.connection() as c:
            row = await (await c.execute('SELECT customer_id FROM banking.identity_links WHERE issuer=%s AND subject=%s',
                                        (issuer, subject))).fetchone()
        if not row:
            raise HTTPException(401, 'Identity is not provisioned')
        return str(row['customer_id'])

    @asynccontextmanager
    async def session(self, context, worker=False, *, readonly=False):
        await self.sessions.revalidate(context)
        # No caller-supplied DSN, role, SQL statement or tenant context override.
        pool=self.worker if worker else self.runtime
        async with pool.connection() as c:
            # Waiting for a pooled connection cannot extend a logged-out session.
            await self.sessions.revalidate(context)
            async with c.transaction():
                if readonly:
                    await c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                await c.execute("SELECT set_config('bank.customer_id',%s,true)", (context.customer_id,))
                yield c

    async def list_conversations(self, context):
        async with self.session(context) as c:
            return await (await c.execute('SELECT id,created_at FROM banking.conversations WHERE customer_id=%s ORDER BY created_at,id',
                                          (context.customer_id,))).fetchall()

    async def create_conversation(self, context):
        async with self.session(context) as c:
            row = await (await c.execute('INSERT INTO banking.conversations(customer_id) VALUES(%s) RETURNING id,created_at',
                                         (context.customer_id,))).fetchone()
            await c.execute("INSERT INTO banking.audit_events(customer_id,conversation_id,event) VALUES(%s,%s,'conversation.created')",
                            (context.customer_id,row['id']))
            return row

    async def conversation(self, context, resource_id, *, worker=False):
        try:
            resource_id = UUID(str(resource_id))
        except ValueError:
            raise HTTPException(404, 'Not found') from None
        async with self.session(context,worker=worker) as c:
            row = await (await c.execute('SELECT id,created_at FROM banking.conversations WHERE id=%s AND customer_id=%s',
                                         (resource_id, context.customer_id))).fetchone()
        if not row:
            raise HTTPException(404, 'Not found')
        return row
