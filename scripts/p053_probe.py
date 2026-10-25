"""Source-blind T05.3 operator: actual guarded HTTP/SQL; no paid calls."""
import asyncio,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import psycopg
from evals.banking_world import database_dsn
from evals.continuity_world import ContinuityWorld
from scripts.p052_probe import ConfirmationProbe

class ContinuityProbe(ConfirmationProbe):
    async def start(self):
        return await super().start(world_class=ContinuityWorld,continuity_enabled=True)
    async def step(self,step):
        s=self.resolve(step);op=s['op'];actor=s.get('actor','A');client=self.clients[actor]
        if op=='oracle':
            r=await super().step(step)
            with psycopg.connect(self.world.admin_dsn) as c:
                r['intents']=c.execute('SELECT proposal_id::text,operation_key::text,request_hash FROM banking.support_submission_intents ORDER BY created_at').fetchall()
                r['runtime_can_advance']=c.execute("SELECT has_function_privilege(%s,'banking.fixture_advance_support_case(uuid,text)','EXECUTE')",(self.world.config['roles']['runtime'],)).fetchone()[0]
                r['case_statuses']=c.execute('SELECT id::text,status,status_updated_at FROM banking.support_cases ORDER BY created_at').fetchall()
            return r
        if op=='advance':
            self.world.guard()
            if s['status'] not in ('created','in_review','resolved'):raise ValueError('Unsupported fixture state')
            with psycopg.connect(database_dsn(self.world.config['dsns']['migrator'],self.world.name)) as c:
                c.execute("SELECT set_config('bank.customer_id',%s,true)",(self.contexts[actor].customer_id,))
                c.execute('SELECT banking.fixture_advance_support_case(%s,%s)',(s['id'],s['status']))
            return {'fixture_advanced':s['status']}
        if op in ('outcome','reconcile','cases','case','case_write'):
            path='/api/v1/support/'
            if op in ('outcome','reconcile'):path+='proposals/'+s['id']+'/'+op
            elif op=='cases':path+='cases'
            else:path+='cases/'+s['id']
            response=await client.request('POST' if op=='reconcile' else 'PATCH' if op=='case_write' else 'GET',path,
                json=s.get('body'),params=s.get('params'),headers=s.get('headers',{}))
            r={'status':response.status_code,'body':response.json()}
            if s.get('save'):self.saved[s['save']]=r['body']
            return r
        return await super().step(step)

async def main():
    request=json.loads(sys.stdin.read());p=await ContinuityProbe().start()
    try:print(json.dumps({'results':[await p.step(s) for s in request['steps']],'oracle':await p.step({'op':'oracle'})},default=str))
    finally:await p.close()
if __name__=='__main__':asyncio.run(main())
