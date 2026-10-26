"""Actual guarded PG16 dump/restore, migration and compatible rollback rehearsal."""
from pathlib import Path
import asyncio,hashlib,json,os,subprocess,sys,uuid
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import psycopg
from psycopg import sql
from evals.banking_world import database_dsn,digest
from evals.continuity_world import ContinuityWorld
from scripts.p053_probe import ContinuityProbe
from store.banking_migrate import apply


def snapshot(world):
    world.guard();tables={}
    with psycopg.connect(world.admin_dsn) as c:
        names=[r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='banking' ORDER BY tablename")]
        for name in names:
            rows=[r[0] for r in c.execute(sql.SQL('SELECT row_to_json(t)::text FROM banking.{} t').format(sql.Identifier(name)))]
            tables[name]={'rows':len(rows),'sha256':digest(sorted(rows))}
    return tables


async def run(folder):
    p=await ContinuityProbe().start();target=None
    report={'task':'T06.4','kind':'actual-guarded-backup-restore','status':'running','paid_model_calls':0,'ordinary_demo_reset':False}
    try:
        world=p.world;report['identity']=world.identity();world.guard()
        turn=await p.step({'op':'turn'});assert turn['status']==202
        preview=await p.step({'op':'draft','body':{'source_turn_id':turn['body']['turn_id'],'topic':'general','summary':'Synthetic release recovery rehearsal'}});assert preview['status']==201
        preview=preview['body'];body={'version':preview['version'],'payload_hash':preview['payload_hash'],'operation_key':str(uuid.uuid4())}
        created=await p.step({'op':'confirm','id':preview['id'],'body':body});assert created['status']==200
        before=await p.step({'op':'oracle'});assert set(before['counts'].values())=={1}
        migrator=database_dsn(world.config['dsns']['migrator'],world.name)
        def rollout(enabled):
            world.guard()
            with psycopg.connect(migrator) as c:
                generation=c.execute('SELECT generation FROM banking.memory_rollout').fetchone()[0]
                return c.execute('SELECT banking.set_memory_rollout(%s,%s)',(enabled,generation)).fetchone()[0]
        rollout(False)
        try:
            response=await p.clients['A'].post('/api/v1/preferences/capture',json={'client_event_id':str(uuid.uuid4()),'text':'Always reply in Hindi'})
            assert response.status_code==503
            assert (await p.clients['A'].get('/api/v1/preferences')).status_code==200
            assert (await p.step({'op':'case','id':created['body']['case_id']}))['status']==200
            assert await p.step({'op':'confirm','id':preview['id'],'body':body})==created
            after=await p.step({'op':'oracle'});assert before['counts']==after['counts']
        finally:
            # This fixture has no legacy cutover attestation. Preserve the real
            # guard refusal and leave the rehearsal safely disabled.
            try:rollout(True)
            except psycopg.errors.ObjectNotInPrerequisiteState:report['reenable_refused_without_legacy_evidence']=True
            else:raise AssertionError('Missing legacy evidence incorrectly allowed re-enable')
        report['rollback']={'new_preference_write_http':503,'compatible_reads_http':200,'confirmed_case_replay_unchanged':True,'one_case_audit_operation_outbox':True,'method':'database feature disable; re-enable correctly refused without legacy evidence; no binary downgrade'}
        report['bank_data_unchanged']=(await p.step({'op':'oracle'}))['bank_data_unchanged'];assert report['bank_data_unchanged'] and p.model_calls==0
        # Capture after closing clients/sessions so no background task changes the dump.
        await p.close();p=None
        before=snapshot(world)
        container=world.config['containers']['postgres'];owner='p01_owner';dump=folder/'banking.dump'
        world.identity();world.guard()
        with dump.open('xb') as output:
            process=subprocess.run(['docker','exec',container,'pg_dump','-U',owner,'--format=custom','--schema=banking','--dbname',world.name],stdout=output,stderr=subprocess.PIPE,timeout=90)
        assert process.returncode==0,'Owned-world pg_dump failed'
        target=ContinuityWorld.create(world.config);target.identity();target.guard()
        assert target.name!=world.name
        with psycopg.connect(target.admin_dsn,autocommit=True) as c:c.execute('DROP SCHEMA banking CASCADE')
        with dump.open('rb') as input_stream:
            restored=subprocess.run(['docker','exec','-i',container,'pg_restore','-U',owner,'--exit-on-error','--dbname',target.name],stdin=input_stream,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120)
        (folder/'restore-private.stderr').write_bytes(restored.stderr)
        assert restored.returncode==0,'Owned-world pg_restore failed'
        assert snapshot(target)==before,'Restored rows differ from source snapshot'
        assert snapshot(world)==before,'Source changed during restoration'
        with psycopg.connect(database_dsn(target.config['dsns']['migrator'],target.name)) as c:apply(c,target.config['roles'],through='0032')
        assert snapshot(target)==before,'Reapplying migrations changed restored rows'
        with psycopg.connect(target.admin_dsn) as c:
            roles=c.execute('SELECT rolname,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=ANY(%s)',(list(target.config['roles'].values()),)).fetchall()
            assert len(roles)==5 and all(not a and not b for _,a,b in roles)
            assert c.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='banking.support_cases'::regclass").fetchone()[0]==target.config['roles']['migrator']
        report.update(status='passed',source_world=world.name,restored_world=target.name,snapshot=before,dump_sha256=hashlib.sha256(dump.read_bytes()).hexdigest(),
            restored_equal=True,source_unchanged=True,migration_reapply_idempotent=True,roles_and_owner_preserved=True,
            limits=['Same verified local cluster, guarded banking schema dump only; roles already exist; not full disaster recovery or Redis backup.','Restored feature remains disabled; re-enable requires separately verified legacy evidence. Feature rollback retains committed support effects and exact same-key replay; no external service delivery or binary downgrade claim.','Private dump/raw evidence retained locally; no normal/shared database dropped.'])
        return report
    finally:
        if p is not None:await p.close()


def main():
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args()
    os.environ.update(PYTHONUTF8='1',LITELLM_LOCAL_MODEL_COST_MAP='True')
    folder=ROOT/'.genesis/local'/('p064-recovery-'+uuid.uuid4().hex);folder.mkdir(parents=True)
    path=ROOT/('.genesis/evidence/T06.4-recovery-'+uuid.uuid4().hex+'.json')
    report={'task':'T06.4','status':'failed'}
    try:report=asyncio.run(run(folder));return 0
    except Exception as error:
        import traceback
        (folder/'failure-private.log').write_text(traceback.format_exc(),encoding='utf-8')
        report['error']=type(error).__name__;report['private_failure_log']=str(folder/'failure-private.log');return 1
    finally:
        report['private_run_folder']=str(folder)
        path.write_text(json.dumps(report,indent=2,default=str)+'\n',encoding='utf-8');print(json.dumps({'status':report['status'],'report':str(path)}))


if __name__=='__main__':raise SystemExit(main())
