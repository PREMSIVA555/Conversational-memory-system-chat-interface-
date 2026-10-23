"""Internal durable run authority; all writes use bounded database functions."""
import hashlib
from psycopg.types.json import Jsonb
from store.banking_preferences import binding

class RunStore:
    def __init__(self,database): self.database=database
    async def claim(self,context,run_id,request,seconds=10):
        async with self.database.session(context) as c:
            return await (await c.execute('SELECT * FROM banking.claim_account_run(%s,%s,%s,%s)',
                (run_id,binding(context),hashlib.sha256(request.encode()).hexdigest(),seconds))).fetchone()
    async def reserve(self,context,row,kind):
        async with self.database.session(context) as c:
            return (await (await c.execute('SELECT banking.reserve_account_attempt(%s,%s,%s,%s) AS n',
                (row['id'],binding(context),row['generation'],kind))).fetchone())['n']
    async def record(self,context,row,attempt,usage):
        async with self.database.session(context) as c:
            await c.execute('SELECT banking.record_account_usage(%s,%s,%s)',(row['id'],attempt,Jsonb(usage)))
    async def publish(self,context,row,result):
        async with self.database.session(context) as c:
            return (await (await c.execute('SELECT banking.publish_account_run(%s,%s,%s,%s) AS ok',
                (row['id'],binding(context),row['generation'],Jsonb(result)))).fetchone())['ok']
    async def usage(self,context,row):
        async with self.database.session(context) as c:
            return await (await c.execute('SELECT attempt,generation,kind,usage FROM banking.account_attempts WHERE run_id=%s ORDER BY attempt',(row['id'],))).fetchall()
