"""Real API-path T03.4 scenario runner; only raw model transport is scripted.

No paid model calls, production reset, fake authorization, or benchmark-quality
claim. All worlds and failed evidence are retained for independent inspection.
"""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime,timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
from types import SimpleNamespace as NS
from urllib.parse import urlsplit
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import httpx
import psycopg
import redis
import uvicorn
from api.foundation import create_app
from evals.banking_world import BankingWorld,UnsafeTarget,database_dsn,digest,FIXTURE

class RawScript:
    """Deterministic SDK transport; never fabricates bank/tool/API responses."""
    def __init__(self):self.outputs=[];self.calls=[];self.delay=0
    def set(self,*outputs,delay=0):self.outputs=list(outputs);self.delay=delay
    async def acompletion(self,**kwargs):
        self.calls.append(dict(at=datetime.now(timezone.utc).isoformat()))
        await asyncio.sleep(self.delay)
        if not self.outputs:raise RuntimeError('Unscripted model dispatch is forbidden')
        value=self.outputs[0] if len(self.outputs)==1 else self.outputs.pop(0)
        if isinstance(value,Exception):raise value
        return NS(choices=[NS(message=NS(content=value if isinstance(value,str) else json.dumps(value)))],model='scripted-raw-transport',usage=None)

@asynccontextmanager
async def serve(world,transport):
    app=create_app(world.settings,model_transport=transport,turns_enabled=True,turn_worker_paused=True,turn_run_seconds=15)
    # Existing provider clock seam affects domain-date ranges only. Redis expiry,
    # source revisions, persisted UTC deadlines and monotonic budgets remain real.
    domain=datetime.fromisoformat(world.fixture['domain_time'])
    app.state.turn_worker.provider._clock=lambda:domain
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='critical',access_log=False,
        ssl_certfile=str(ROOT/'.genesis/local/p01/tls.crt'),ssl_keyfile=str(ROOT/'.genesis/local/p01/tls.key')))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(500):
        if server.started:break
        if task.done():await task
        await asyncio.sleep(.01)
    if not server.started:raise RuntimeError('Actual TLS API startup failed')
    try:yield app,f'https://127.0.0.1:{port}'
    finally:
        server.should_exit=True
        try:await asyncio.wait_for(task,8)
        except TimeoutError:
            task.cancel();await asyncio.gather(task,return_exceptions=True)

async def real_login(client,user,expected_owner,world):
    start=await client.get('/api/v1/auth/login');assert start.status_code==303
    location=start.headers['location'];assert 'code_challenge_method=S256' in location
    async with httpx.AsyncClient(verify=world.settings.oidc_ca_file,timeout=15,follow_redirects=False) as issuer:
        form=await issuer.get(location)
        action=re.search(r'<form[^>]+action="([^"]+)"',form.text)
        assert action,'Actual Keycloak form unavailable'
        result=await issuer.post(html.unescape(action[1]),data={'username':user['username'],'password':user['password'],'credentialId':''})
        assert result.status_code in (302,303),'Actual Keycloak login failed'
    # Registered redirect remains fixed. Only the browser delivery bridge points
    # to this trial's real TLS callback controller, preserving query and flow cookie.
    target=result.headers['location']
    assert target.startswith(world.settings.redirect_uri+'?')
    callback=await client.get('/api/v1/auth/callback?'+urlsplit(target).query)
    assert callback.status_code==303,'Actual callback/code exchange failed'
    session=await client.get('/api/v1/auth/session');assert session.status_code==200
    assert session.json()['customer_id']==expected_owner
    client.headers.update({'Origin':world.settings.origin,'X-CSRF-Token':session.json()['csrf_token']})
    return dict(method='Keycloak authorization-code PKCE S256',customer_id=expected_owner,callback_status=callback.status_code,session_status=session.status_code)

def bank_plan(tool,**args):return dict(tool=tool,args=args)

def money(minor,currency):
    # Independent oracle formatter uses integer arithmetic, never app Money.
    exponent={'INR':2,'USD':2}[currency];n=int(minor);sign='-' if n<0 else '';n=abs(n)
    return f'{currency} {sign}{n//10**exponent}.{n%10**exponent:0{exponent}d}'

def grade_evidence(result,oracle,kind,account):
    assert result['status']=='completed',result['status']
    assert result['evidence'],'Financial answer lacks checked evidence'
    accounts={r[0]:r for r in oracle['accounts']};tx={r[0]:r for r in oracle['transactions']}
    assert account in accounts
    seen=set()
    for e in result['evidence']:
        assert e['authority']=='fresh_authorized_bank' and e['as_of'] and e['version']
        if e['ref'] in accounts:
            row=accounts[e['ref']];assert e['ref']==account
            expected={'minor_units':row[4 if e['field']=='current_balance' else 5],'currency':row[3],'exponent':2}
            assert e['field'] in ('current_balance','available_balance')
            assert e['value']==expected
            assert str(e['version'])==row[6]
            assert datetime.fromisoformat(e['as_of'])==datetime.fromisoformat(row[7])
            assert money(expected['minor_units'],row[3]) in result['text']
            seen.add(e['field'])
        elif e['ref'] in tx:
            row=tx[e['ref']];assert row[1]==account
            expected={'account_id':row[1],'amount':{'minor_units':row[3],'currency':accounts[account][3],'exponent':2},'status':row[4],'booked_on':row[5]}
            assert e['field'] in expected and e['value']==expected[e['field']]
            assert str(e['version'])==row[7]
            assert datetime.fromisoformat(e['as_of'])==datetime.fromisoformat(row[8])
            assert row[0] in result['text'] and row[4] in result['text'] and money(row[3],accounts[account][3]) in result['text']
            seen.add(row[0])
        else:
            assert e['ref']=='history' and e['field'] in ('complete','start','end')
    if kind=='balance':assert seen=={'current_balance','available_balance'}
    if kind=='history':
        expected=[r[0] for r in oracle['transactions'] if r[1]==account]
        assert seen==set(expected)
        positions=[result['text'].index(i) for i in expected];assert positions==sorted(positions)
        assert 'reason' not in result['text'].lower() and 'because' not in result['text'].lower()

def timing_receipt(world,turnid):
    with psycopg.connect(world.admin_dsn) as c:
        events=c.execute('SELECT event_id,event_type,created_at FROM banking.turn_events WHERE turn_id=%s ORDER BY event_id',(turnid,)).fetchall()
        row=c.execute('SELECT r.started_at,r.deadline,r.model_calls,r.tool_calls,r.model_calls+r.tool_calls FROM banking.account_runs r JOIN banking.turns t ON t.run_id=r.id WHERE t.id=%s',(turnid,)).fetchone()
        attempts=c.execute('SELECT a.attempt,a.kind,a.reserved_at,a.usage FROM banking.account_attempts a JOIN banking.turns t ON t.run_id=a.run_id WHERE t.id=%s ORDER BY a.attempt',(turnid,)).fetchall()
    accepted=events[0][2];running=next((e[2] for e in events if e[1]=='turn.running'),None);terminal=events[-1][2]
    assert running and terminal>=running>=accepted
    assert 0<=row[2]<=3 and 0<=row[3]<=2 and row[4]<=6
    return dict(events=[dict(id=e[0],type=e[1],at=e[2].isoformat()) for e in events],
        queue_seconds=(running-accepted).total_seconds(),run_seconds=(terminal-running).total_seconds(),full_seconds=(terminal-accepted).total_seconds(),
        logical_budget_started_at=row[0].isoformat(),deadline=row[1].isoformat(),
        model_attempts=row[2],tool_attempts=row[3],total_attempts=row[4],
        attempts=[dict(attempt=a[0],kind=a[1],at=a[2].isoformat(),usage=a[3]) for a in attempts],
        token_usage='unavailable: scripted raw provider omits usage',cost='unknown; no paid calls',
        timing_definition='accepted event to first running = queue; first running to terminal = run; accepted to terminal = full; logical budget starts at acceptance')

async def scenarios(world,folder):
    script=RawScript();results=[];f=world.fixture;oracle=world.oracle();a=f['accounts'][0]['id']
    async with serve(world,script) as (app,base):
        async with httpx.AsyncClient(base_url=base,verify=world.settings.oidc_ca_file,timeout=20) as c,httpx.AsyncClient(base_url=base,verify=world.settings.oidc_ca_file,timeout=20) as b:
            auth=[await real_login(c,world.config['users'][0],f['customers'][0],world),await real_login(b,world.config['users'][1],f['customers'][1],world)]
            conversation=await c.post('/api/v1/conversations',json={});assert conversation.status_code==201
            path='/api/v1/conversations/'+conversation.json()['id']+'/turns'
            remembered=await c.post('/api/v1/memory',json=dict(client_event_id=str(uuid4()),conversation_id=conversation.json()['id'],text='Historical balance for '+a+' was INR 999.00. This is user-provided history.'))
            assert remembered.status_code==201,'Actual authorized memory setup failed'
            memory=await c.get('/api/v1/memory',params={'conversation_id':conversation.json()['id']})
            assert memory.status_code==200 and memory.json()['items'] and memory.json()['authoritative_bank_facts'] is False
            # A selected product is validated before acceptance. A real bank
            # outage here must be bounded and must not create a partial turn.
            rejected_id=str(uuid4())
            with psycopg.connect(world.admin_dsn) as blocker:
                blocker.execute('LOCK TABLE banking.accounts IN ACCESS EXCLUSIVE MODE')
                began=time.monotonic()
                rejected=await c.post(path,json=dict(client_turn_id=rejected_id,text='Read balance for '+a,
                    selection=dict(account_id=a,version='1')))
                elapsed=time.monotonic()-began
                assert rejected.status_code==503 and elapsed<8,('pre-acceptance bank outage',rejected.status_code,elapsed)
                assert (await c.get(path+'/by-client/'+rejected_id)).status_code==404
            pre_acceptance_fault=dict(status=rejected.status_code,elapsed_seconds=elapsed,
                recovered_turn_status=404,partial_turn_created=False,
                fault='actual isolated accounts table lock before selected-product validation')
            (folder/'pre-acceptance-outage.json').write_text(json.dumps(pre_acceptance_fault,indent=2),encoding='utf-8')
            async def run(name,text,outputs,expect,kind=None,selection=None,queued=False,account=None):
                app.state.turn_worker.paused=True;script.set(*outputs)
                payload=dict(client_turn_id=str(uuid4()),text=text)
                if selection:payload['selection']=selection
                r=await c.post(path,json=payload);assert r.status_code==202,r.text
                accepted=r.json()
                # The durable row must be recoverable before execution begins.
                replay=await c.post(path,json=payload);assert replay.status_code==202 and replay.json()['turn_id']==accepted['turn_id']
                assert (await c.post(path,json={**payload,'text':text+' changed'})).status_code==409
                assert (await b.get(accepted['status_url'])).status_code==404
                if queued:await asyncio.sleep(.25)
                app.state.turn_worker.paused=False
                for _ in range(600):
                    status=await c.get(accepted['status_url']);assert status.status_code==200
                    if status.json()['result'] is not None:break
                    await asyncio.sleep(.025)
                done=status.json();assert done['result'] is not None,'No terminal within bounded wait'
                result=done['result'];assert result['status'] in expect,(name,result)
                assert '999.00' not in result['text'],'Historical financial value leaked into current answer'
                if kind:grade_evidence(result,oracle,kind,account or a)
                else:assert not result['evidence'] or name=='clarification'
                stream=await c.get(accepted['events_url']);assert stream.status_code==200
                ids=[int(x[4:]) for x in stream.text.splitlines() if x.startswith('id: ')]
                assert ids==sorted(set(ids)) and ids[0]==1
                resume=await c.get(accepted['events_url'],headers={'Last-Event-ID':str(ids[-2])})
                assert [int(x[4:]) for x in resume.text.splitlines() if x.startswith('id: ')]==[ids[-1]]
                record=dict(case=name,input=payload,http_status=r.status_code,turn_id=accepted['turn_id'],result=result,
                    timing=timing_receipt(world,accepted['turn_id']),sse_ids=ids,recovery='same ID replay, changed payload409, foreign404, persisted reconnect')
                if queued:assert record['timing']['queue_seconds']>=.2
                results.append(record)
                (folder/'transcript.json').write_text(json.dumps(dict(authentication=auth,scenarios=results),indent=2,default=str),encoding='utf-8')
                return result
            clarified=await run('clarification','What is my balance?',[bank_plan('list_accounts')],{'clarify'})
            assert {e['ref'] for e in clarified['evidence']}=={x['id'] for x in f['accounts'] if x['owner']==0}
            selected=dict(account_id=a,version='1')
            await run('selected-balance','Show current and available balances for '+a,[bank_plan('get_account',account_id=a)],{'completed'},'balance',selected,True)
            second=f['accounts'][1]['id']
            await run('second-product-zero-negative','Show current and available balances for '+second,[bank_plan('get_account',account_id=second)],{'completed'},'balance',dict(account_id=second,version='1'),account=second)
            await run('history','Show history for '+a,[bank_plan('list_transactions',account_id=a)],{'completed'},'history',selected)
            bad=bank_plan('get_account',account_id=a);bad['draft']={'text':'Your balance is INR 999.00','claims':[]}
            result=await run('wrong-draft','Check exact balance for '+a,[bad],{'completed'},'balance',selected)
            assert result['draft_accepted'] is False and '999.00' not in result['text']
            await run('foreign-account','Read foreign account '+f['accounts'][2]['id'],[bank_plan('get_account',account_id=f['accounts'][2]['id'])],{'no_evidence','unavailable'})
            await run('unknown-account','Read unknown account 99999999-9999-4999-8999-999999999999',[bank_plan('get_account',account_id='99999999-9999-4999-8999-999999999999')],{'no_evidence','unavailable'})
            await run('unsupported','Transfer money',[bank_plan('unsupported')],{'unsupported'})
            await run('malformed','Bad plan',['{bad json','{bad json'],{'invalid_plan'})
            await run('unknown-tool','Unknown action',[dict(tool='execute_sql',args={'sql':'DROP TABLE accounts'})]*2,{'invalid_plan'})
            await run('model-outage','Model transport unavailable',[RuntimeError('Synthetic raw provider unavailable')],{'unavailable'})
            # Actual SQL read outage: isolated trial accounts table is locked.
            # No provider/API method is replaced, and other databases are untouched.
            with psycopg.connect(world.admin_dsn) as blocker:
                blocker.execute('LOCK TABLE banking.accounts IN ACCESS EXCLUSIVE MODE')
                await run('bank-outage','History says balance INR 999.00. Read current '+a,[bank_plan('get_account',account_id=a)],{'unavailable','expired'})
            # A scriptable state change updates the real isolated bank authority.
            with psycopg.connect(world.admin_dsn) as db:
                db.execute('UPDATE banking.accounts SET current_minor=12345,available_minor=12000 WHERE id=%s',(a,))
            oracle=world.oracle()
            await run('changed-bank-authority','Recheck balance for '+a,[bank_plan('get_account',account_id=a)],{'completed'},'balance')
            # Frozen domain date must not freeze Redis security expiry.
            token=c.cookies.get('__Host-bank_sid');key=app.state.sessions.key('session',token)
            await app.state.sessions.command('pexpire',key,40);await asyncio.sleep(.08)
            assert (await c.get('/api/v1/auth/session')).status_code==401
            for client in [c,b]:
                token=client.cookies.get('__Host-bank_sid')
                if token:await app.state.sessions.command('delete',app.state.sessions.key('session',token))
    return dict(case_count=len(results),pass_count=len(results),failed=0,pre_acceptance_fault=pre_acceptance_fault,
        domain_time=f['domain_time'],security_expiry_uses_real_clock=True,
        script_label='deterministic raw SDK transport, real auth/API/worker/DB; no live model quality evaluation',oracle=oracle)

def world_checks(world):
    h1=world.reset();h2=world.reset();assert h1==h2
    try:world.reset(world.settings.runtime_dsn)
    except (UnsafeTarget,psycopg.Error):runtime_denied=True
    else:raise AssertionError('Runtime reset unexpectedly allowed')
    with psycopg.connect(world.settings.runtime_dsn) as runtime:
        try:
            with runtime.transaction():runtime.execute('DROP SCHEMA banking CASCADE')
        except psycopg.errors.InsufficientPrivilege:runtime_sql_denied=True
        else:raise AssertionError('Runtime SQL reset unexpectedly allowed')
    unsafe=BankingWorld(world.config,'bank_p01',world.marker)
    try:unsafe.reset()
    except UnsafeTarget:protected=True
    else:raise AssertionError('Non-test reset unexpectedly allowed')
    other=BankingWorld.create(world.config)
    # Parallel independent reset calls and real contamination canaries.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(w.reset) for w in [world,other]]
        assert [x.result() for x in futures]==[h1,h1]
    with psycopg.connect(world.admin_dsn) as c:
        c.execute('UPDATE banking.accounts SET current_minor=555 WHERE id=%s',(world.fixture['accounts'][0]['id'],))
    with redis.Redis.from_url(world.settings.redis_url,decode_responses=True) as r:
        r.set(world.namespace+':canary','world-A')
        assert r.get(other.namespace+':canary') is None
    (world.folder/'canary.txt').write_text('world-A',encoding='utf-8')
    assert not (other.folder/'canary.txt').exists() and other.semantic_hash()==h1 and world.semantic_hash()!=h1
    assert world.reset()==h1 and other.reset()==h1
    return dict(first_reset=h1,second_reset=h2,parallel_reset_hashes_match=True,db_redis_file_isolation=True,
        runtime_reset_denied=runtime_denied,runtime_sql_reset_denied=runtime_sql_denied,protected_target_refused=protected,trial_databases=[world.name,other.name],retained=True)

async def execute(folder):
    from scripts.p03_check import hashes
    def pinned_hashes():
        values=hashes()
        for p in [FIXTURE,ROOT/'evals/banking_world.py',ROOT/'.genesis/sources/P03_SOURCE.md',ROOT/'.genesis/sources/P03_SOURCE.json']:
            values[p.relative_to(ROOT).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
        return dict(sorted(values.items()))
    pinned=pinned_hashes()
    provenance=json.loads((ROOT/'.genesis/sources/P03_SOURCE.json').read_text(encoding='utf-8'))['manifest']
    source_digest=hashlib.sha256(Path(provenance['source_path']).read_bytes()).hexdigest()
    assert source_digest==provenance['source_sha256'],'Authoritative plan digest changed'
    config=json.loads((ROOT/'.genesis/local/p01/config.json').read_text(encoding='utf-8'))
    world=BankingWorld.create(config)
    (folder/'trial.json').write_text(json.dumps(dict(database=world.name,namespace=world.namespace,manifest=str(world.folder/'manifest.json'),fixture_version=world.fixture['version'])),encoding='utf-8')
    identity=world.identity();checks=await asyncio.to_thread(world_checks,world)
    (folder/'world-checks.json').write_text(json.dumps(checks,indent=2),encoding='utf-8')
    # Capability metadata can require a cold SDK import. Pin this benchmark's
    # setup separately; it never dispatches a completion or extends a turn budget.
    from llm.config import bounded_completion_capabilities
    began=time.monotonic()
    capabilities=await bounded_completion_capabilities(timeout=60)
    assert capabilities['structured_json'],'Configured model lacks the required JSON capability'
    metadata_setup=dict(seconds=time.monotonic()-began,capabilities=capabilities,
        label='Actual SDK capability metadata warm-up before trial acceptance; no model completion; excluded from request latency')
    (folder/'metadata-setup.json').write_text(json.dumps(metadata_setup,indent=2),encoding='utf-8')
    result=await scenarios(world,folder)
    assert pinned_hashes()==pinned,'Source changed during scenario trial; result incomplete'
    assert hashlib.sha256(Path(provenance['source_path']).read_bytes()).hexdigest()==source_digest
    from importlib.metadata import version
    versions={name:version(name) for name in ['fastapi','psycopg','redis','httpx','uvicorn','litellm','pydantic','Authlib']}
    return dict(task='T03.4',passed=True,identity=identity,world_checks=checks,metadata_setup=metadata_setup,scenario=result,
        fixture_sha256=hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),pinned_sources=pinned,
        source_unchanged=True,authoritative_source=dict(path=provenance['source_path'],sha256=source_digest),
        python_version=sys.version,dependency_versions=versions,
        policy_version='P03 source contract + strict account-enquiry registry, no external policy retrieval',
        model_version='scripted-raw-transport/v1; configured model identity recorded per attempt, no remote inference',
        real_execution_utc=datetime.now(timezone.utc).isoformat(),domain_clock_scope='Existing provider date-range seam only; Redis/session TTL, cursor expiry, revision as_of, UTC deadlines and monotonic budgets use real time',
        limitations=['Raw model behavior scripted; no live model-quality or spend claim.','UI is verified separately in a real browser.','Runtime account-enquiry path only; P04+ excluded.'])

def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('output',type=Path);parser.add_argument('--task',choices=['T03.4'],default='T03.4');args=parser.parse_args()
    folder=args.output.resolve()
    if not folder.is_relative_to((ROOT/'evidence/scenarios').resolve()):raise SystemExit('Evidence target must be under evidence/scenarios')
    folder.mkdir(parents=True,exist_ok=False)
    os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP','True')
    try:result=asyncio.run(execute(folder))
    except BaseException as e:
        # Never persist exception messages containing request URLs or credentials.
        (folder/'report.json').write_text(json.dumps(dict(task='T03.4',passed=False,error_type=type(e).__name__,claim='Failed/blocked trial, not passing benchmark'),indent=2),encoding='utf-8')
        raise
    (folder/'report.json').write_text(json.dumps(result,indent=2,default=str),encoding='utf-8')
    files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}
    (folder/'manifest.json').write_text(json.dumps(files,indent=2),encoding='utf-8')
    print(json.dumps(dict(task='T03.4',passed=True,scenario_count=result['scenario']['case_count'],evidence=str(folder))))
if __name__=='__main__':main()
