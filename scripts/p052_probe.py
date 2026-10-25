"""T05.2 behavioral operator: private actual HTTP/SQL, no paid transport."""
import asyncio,json,sys
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import psycopg
from psycopg import sql
from scripts.p051_probe import Probe
from evals.confirmation_world import ConfirmationWorld

TABLES=('support_cases','support_action_audit','support_operations','support_outbox')
class ConfirmationProbe(Probe):
    async def start(self):
        await super().start(world_class=ConfirmationWorld,confirm_enabled=True)
        self.telemetry_failures=0
        return self
    async def step(self,step):
        s=self.resolve(step);op=s['op']
        if op=='telemetry_fault':
            def failed_sink(record):
                self.telemetry_failures+=1
                raise RuntimeError('Synthetic optional exporter failure')
            self.app.state.telemetry.exporter.sink=failed_sink
            return {'optional_exporter_fault':True}
        if op=='telemetry_flush':
            await asyncio.to_thread(self.app.state.telemetry.provider.force_flush,2000)
            return {'optional_exporter_failures':self.telemetry_failures}
        if op=='key':
            value=str(uuid4())
            if s.get('save'):self.saved[s['save']]=value
            return value
        if op=='oracle':
            result=await super().step({'op':'oracle'})
            with psycopg.connect(self.world.admin_dsn) as c:
                result['counts']={t:c.execute(sql.SQL('SELECT count(*) FROM banking.{}').format(sql.Identifier(t))).fetchone()[0] for t in TABLES}
                result['cases']=c.execute('SELECT id::text,customer_id::text,source_turn_id::text,proposal_id::text,case_number,status,payload IS NULL FROM banking.support_cases ORDER BY created_at').fetchall()
                result['operations']=c.execute('SELECT operation_key::text,proposal_id::text,case_id::text,receipt,expires_at<=clock_timestamp() FROM banking.support_operations ORDER BY expires_at').fetchall()
                result['audit']=c.execute('SELECT case_id::text,event FROM banking.support_action_audit ORDER BY case_id').fetchall()
                result['outbox']=c.execute('SELECT case_id::text,event_type,schema_version,status FROM banking.support_outbox ORDER BY case_id').fetchall()
                result['direct_runtime_writes']=c.execute("SELECT has_table_privilege(%s,'banking.support_cases','INSERT,UPDATE,DELETE')",(self.world.config['roles']['runtime'],)).fetchone()[0]
                result['optional_exporter_failures']=self.telemetry_failures
            return result
        if op in ('expire_receipt','fault','clear_fault'):
            self.world.guard()
            with psycopg.connect(self.world.admin_dsn) as c:
                if op=='expire_receipt':
                    c.execute('ALTER TABLE banking.support_operations DISABLE TRIGGER immutable')
                    c.execute("UPDATE banking.support_operations SET expires_at=clock_timestamp()-interval '1 second' WHERE operation_key=%s",(s['key'],))
                    c.execute('ALTER TABLE banking.support_operations ENABLE TRIGGER immutable')
                else:
                    for table in (*TABLES,'support_proposals'):
                        c.execute(sql.SQL('DROP TRIGGER IF EXISTS test_confirm_fault ON banking.{}').format(sql.Identifier(table)))
                    if op=='fault':
                        stage=s['stage'];table='support_outbox' if stage=='commit' else stage
                        if table not in (*TABLES,'support_proposals'):raise ValueError('Unsupported fault')
                        c.execute("CREATE OR REPLACE FUNCTION banking.test_confirm_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Synthetic confirmation storage fault'; END $$")
                        timing='CREATE CONSTRAINT TRIGGER test_confirm_fault AFTER INSERT ON banking.support_outbox DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION banking.test_confirm_fault()' if stage=='commit' else sql.SQL('CREATE TRIGGER test_confirm_fault BEFORE {} ON banking.{} FOR EACH ROW EXECUTE FUNCTION banking.test_confirm_fault()').format(sql.SQL('UPDATE' if table=='support_proposals' else 'INSERT'),sql.Identifier(table))
                        c.execute(timing)
            return {'test_fault':op,'stage':s.get('stage')}
        return await super().step(step)

async def main():
    request=json.loads(sys.stdin.read());p=await ConfirmationProbe().start()
    try:print(json.dumps({'results':[await p.step(s) for s in request['steps']],'oracle':await p.step({'op':'oracle'})},default=str))
    finally:await p.close()
if __name__=='__main__':asyncio.run(main())
