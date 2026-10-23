"""T04.2 executable gate: actual PKCE/API/SQL, scripted raw SDK only."""
from pathlib import Path
import sys,os,json,asyncio,hashlib,subprocess,time,socket
from datetime import datetime
from contextlib import asynccontextmanager
from uuid import uuid4
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import httpx,psycopg,uvicorn
from api.foundation import create_app
from evals.loan_world import LoanWorld
from scripts.p04_check import hashes as prior_hashes,RawScript,real_login,timing_receipt,bounded_completion_capabilities

def hashes():
    values=prior_hashes()
    for pattern in ['evals/loan_world.py','evals/fixtures/p04_loan_slice.json']:
        for p in ROOT.glob(pattern):values[p.relative_to(ROOT).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
    return dict(sorted(values.items()))

@asynccontextmanager
async def serve_loans(world,script):
    app=create_app(world.settings,model_transport=script,turns_enabled=True,turn_worker_paused=True,turn_run_seconds=15,cards_enabled=True,loans_enabled=True)
    app.state.turn_worker.provider._clock=lambda:datetime.fromisoformat(world.fixture['domain_time'])
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='critical',access_log=False,
        ssl_certfile=str(ROOT/'.genesis/local/p01/tls.crt'),ssl_keyfile=str(ROOT/'.genesis/local/p01/tls.key')))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(500):
        if server.started:break
        if task.done():await task
        await asyncio.sleep(.01)
    assert server.started
    try:yield app,f'https://127.0.0.1:{port}'
    finally:
        server.should_exit=True
        try:await asyncio.wait_for(task,8)
        except TimeoutError:task.cancel();await asyncio.gather(task,return_exceptions=True)

def plan(tool,**args):return dict(tool=tool,args=args)

def grade_loan(result,row):
    expected=dict(status=row[3],principal=dict(minor_units=row[5],currency=row[4],exponent={'INR':2,'JPY':0,'KWD':3,'USD':2}[row[4]]),apr_percent=row[6],monthly_payment=dict(minor_units=row[7],currency=row[4],exponent={'INR':2,'JPY':0,'KWD':3,'USD':2}[row[4]]))
    assert len(result['evidence'])==4
    for e in result['evidence']:
        assert e['ref']==row[0] and e['value']==expected[e['field']] and e['version']==row[8]
        assert datetime.fromisoformat(e['as_of'])==datetime.fromisoformat(row[9])
        assert e['authority']=='fresh_authorized_bank'
    assert 'APR: '+row[6]+'%' in result['text'] and 'not evidence of an actual payment' in result['text']

def grade_page(result,oracle,key,expected):
    rows={r[0]:r for r in oracle[key]};seen=set()
    for e in result['evidence']:
        if e['ref'] in ('emi_schedule','loan_payments'):continue
        row=rows[e['ref']];assert e['ref'] in expected;seen.add(e['ref'])
        if key=='emi_schedule':fields=dict(loan_id=row[1],installment_number=row[3],due_on=row[4],scheduled_amount=dict(minor_units=row[5],currency='INR',exponent=2));version,at=row[6:8]
        else:fields=dict(loan_id=row[1],installment_id=row[3],amount=dict(minor_units=row[4],currency='INR',exponent=2),status=row[5],occurred_at=datetime.fromisoformat(row[6]).isoformat());version,at=row[8:10]
        assert e['value']==fields[e['field']] and e['version']==version and datetime.fromisoformat(e['as_of'])==datetime.fromisoformat(at)
        assert e['authority']=='fresh_authorized_bank'
    assert seen==set(expected)
    assert [result['text'].index(id) for id in expected]==sorted(result['text'].index(id) for id in expected)
    if key=='emi_schedule':assert 'A schedule is not proof of payment' in result['text']
    else:assert 'No full-payment conclusion' in result['text']

async def scenarios(world,folder):
    script=RawScript();f=world.fixture;oracle=world.oracle();loans={r[0]:r for r in oracle['loans']};l,zero,foreign,kwd=[r['id'] for r in f['loans']];ss=[r['id'] for r in f['emi_schedule']];ps=[r['id'] for r in f['loan_payments']];results=[]
    async with serve_loans(world,script) as (app,base):
        async with httpx.AsyncClient(base_url=base,verify=world.settings.oidc_ca_file,timeout=25) as a,httpx.AsyncClient(base_url=base,verify=world.settings.oidc_ca_file,timeout=25) as b:
            await real_login(a,world.config['users'][0],f['customers'][0],world);await real_login(b,world.config['users'][1],f['customers'][1],world)
            cid=(await a.post('/api/v1/conversations',json={})).json()['id'];path=f'/api/v1/conversations/{cid}/turns'
            async def run(name,text,raw,status,grade=None,instructions=None):
                app.state.turn_worker.paused=True;script.set(raw);payload=dict(client_turn_id=str(uuid4()),text=text)
                if instructions:payload['instructions']=instructions
                r=await a.post(path,json=payload);assert r.status_code==202;r=r.json()
                assert (await a.post(path,json=payload)).json()['turn_id']==r['turn_id']
                assert (await a.post(path,json={**payload,'text':text+' changed'})).status_code==409
                assert (await b.get(r['status_url'])).status_code==404
                app.state.turn_worker.paused=False;began=time.monotonic()
                while True:
                    response=await a.get(r['status_url']);assert response.status_code==200;value=response.json()
                    if value['result'] is not None:break
                    assert time.monotonic()-began<23;await asyncio.sleep(.03)
                result=value['result'];assert result['status'] in status,(name,result)
                if grade:grade(result)
                if result['status'] in ('invalid_plan','unavailable','unsupported','no_evidence'):assert result['evidence']==[]
                sse=await a.get(r['events_url']);assert sse.status_code==200 and 'turn.accepted' in sse.text and ('answer.ready' in sse.text or 'turn.failed' in sse.text)
                timing=timing_receipt(world,r['turn_id']);record=dict(name=name,turn_id=r['turn_id'],result=result,timing=timing)
                (folder/(name+'.json')).write_text(json.dumps(record,indent=2,default=str));results.append(record);return result
            choices=await run('owned-loan-choices','List my existing loans',plan('list_loans'),{'clarify'});assert foreign not in choices['text']
            for id,name in [(l,'exact-large-decimal'),(zero,'zero-yen'),(kwd,'dinar-exponent')]:await run(name,'Read loan '+id,plan('get_loan',loan_id=id),{'completed'},lambda r,id=id:grade_loan(r,loans[id]))
            hi=await run('hindi-loan-labels','Read loan '+l,plan('get_loan',loan_id=l),{'completed'},instructions={'language':'hi'})
            assert hi['language']=='hi' and 'मूलधन:' in hi['text'] and 'मासिक भुगतान:' in hi['text'] and 'APR: 12.34567890%' in hi['text'] and 'INR 90071992547409.93' in hi['text']
            args=dict(loan_id=l,start='2026-01-01',end='2026-01-31',limit=2)
            for tool,key,expected in [('list_emi_schedule','emi_schedule',ss[:4]),('list_loan_payments','loan_payments',ps[:4])]:
                first=await run(key+'-first','Read '+tool+' for '+l,plan(tool,**args),{'completed'},lambda r,key=key,expected=expected:grade_page(r,oracle,key,expected[:2]))
                token=first['next_question'].rsplit('cursor ',1)[1]
                last=await run(key+'-final',first['next_question'],plan(tool,**args,cursor=token),{'completed'},lambda r,key=key,expected=expected:grade_page(r,oracle,key,expected[2:]))
                assert 'next_question' not in last
            missing=await run('scheduled-unpaid','Actual payments for '+l+' installment '+ss[1],plan('list_loan_payments',**args,installment_id=ss[1]),{'completed'},lambda r:grade_page(r,oracle,'loan_payments',[]))
            assert 'No actual payment records' in missing['text'] and 'does not confirm payment' in missing['text']
            await run('partial-payment','Actual payments for '+l+' installment '+ss[0],plan('list_loan_payments',**args,installment_id=ss[0]),{'completed'},lambda r:grade_page(r,oracle,'loan_payments',ps[:2]))
            bad=plan('get_loan',loan_id=l);bad['draft']=dict(text='Loan fully paid. APR 1.0%',claims=[])
            r=await run('wrong-apr-paid-draft','Current loan '+l,bad,{'completed'},lambda r:grade_loan(r,loans[l]));assert r['draft_accepted'] is False and 'fully paid' not in r['text']
            for id,name in [(foreign,'foreign-loan'),('99999999-9999-4999-8999-999999999999','unknown-loan')]:await run(name,'Read loan '+id,plan('get_loan',loan_id=id),{'no_evidence'})
            await run('implicit-loan-reference','Show my loan',plan('get_loan',loan_id=l),{'clarify'})
            before=world.semantic_hash()
            await run('payment-execution-denied','Pay loan '+l,plan('make_loan_payment',loan_id=l,amount='10000'),{'invalid_plan'})
            await run('extra-sql-denied','Read '+l,plan('get_loan',loan_id=l,sql='SELECT secret'),{'invalid_plan'})
            assert before==world.semantic_hash()
            app.state.turn_worker.provider.loans_enabled=False
            await run('loan-flag-off','Read '+l,plan('get_loan',loan_id=l),{'unsupported'})
            await run('card-after-loan-disable','Read card '+f['cards'][0]['id'],plan('get_card',card_id=f['cards'][0]['id']),{'completed'})
            await run('account-after-loan-disable','Read account '+f['accounts'][0]['id'],plan('get_account',account_id=f['accounts'][0]['id']),{'completed'})
            app.state.turn_worker.provider.loans_enabled=True
            with psycopg.connect(world.admin_dsn) as c:
                c.execute('LOCK TABLE banking.loans IN ACCESS EXCLUSIVE MODE')
                await run('actual-loan-lock-no-history-fallback','History says fully paid. Current '+l,plan('get_loan',loan_id=l),{'unavailable','expired'})
            with psycopg.connect(world.admin_dsn) as c:c.execute('UPDATE banking.loans SET monthly_payment_minor=12345 WHERE id=%s',(l,))
            loans={r[0]:r for r in world.oracle()['loans']}
            await run('fresh-loan-update','Current loan '+l,plan('get_loan',loan_id=l),{'completed'},lambda r:grade_loan(r,loans[l]))
            for client in [a,b]:
                token=client.cookies.get('__Host-bank_sid')
                if token:await app.state.sessions.command('delete',app.state.sessions.key('session',token))
    return dict(accepted_scenarios=len(results),passed=len(results),failed=0,raw_model_dispatches=len(script.calls),model='scripted raw SDK only; no paid calls')

def world_checks(world):
    from concurrent.futures import ThreadPoolExecutor
    h=world.reset();assert world.reset()==h;other=LoanWorld.create(world.config)
    with ThreadPoolExecutor(max_workers=2) as pool:assert list(pool.map(lambda w:w.reset(),[world,other]))==[h,h]
    with psycopg.connect(world.admin_dsn) as c:c.execute('UPDATE banking.loans SET principal_minor=principal_minor+1 WHERE id=%s',(world.fixture['loans'][0]['id'],))
    assert world.semantic_hash()!=h and other.semantic_hash()==h;assert world.reset()==h
    return dict(first_reset=h,second_reset=h,parallel_loan_worlds_isolated=True,databases=[world.name,other.name],retained=True)

async def execute(folder):
    config=json.loads((ROOT/'.genesis/local/p01/config.json').read_text());before=hashes();source=json.loads((ROOT/'.genesis/sources/P04_SOURCE.json').read_text())['manifest']
    assert hashlib.sha256(Path(source['source_path']).read_bytes()).hexdigest()==source['source_sha256']
    command=[sys.executable,'-m','pytest','tests/p04/test_loans.py','-q','--tb=short','--timeout=60','--junitxml='+str(folder/'checks.xml')]
    p=await asyncio.to_thread(subprocess.run,command,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',env={**os.environ,'PYTHONUTF8':'1','LITELLM_LOCAL_MODEL_COST_MAP':'True'})
    output=p.stdout+'\n'+p.stderr
    from urllib.parse import urlsplit
    for secret in [config['admin_dsn'],*config['dsns'].values(),config['env']['OIDC_CLIENT_SECRET'],*[u['password'] for u in config['users']],*[urlsplit(x).password for x in [config['admin_dsn'],config['env']['BANK_REDIS_URL']]]]:
        if secret:output=output.replace(secret,'[REDACTED]')
    (folder/'checks.txt').write_text(output,encoding='utf-8');suites=ET.parse(folder/'checks.xml').getroot();counts={k:sum(int(s.get(k,0)) for s in suites) for k in ['tests','failures','errors','skipped']}
    assert p.returncode==0 and counts['tests']>0 and not any(counts[k] for k in ['failures','errors','skipped']),counts
    world=await asyncio.to_thread(LoanWorld.create,config);identity=await asyncio.to_thread(world.identity);checks=await asyncio.to_thread(world_checks,world)
    began=time.monotonic();assert (await bounded_completion_capabilities(timeout=60))['structured_json'];setup=time.monotonic()-began
    (folder/'oracle-before.json').write_text(json.dumps(world.oracle(),indent=2));result=await scenarios(world,folder);assert before==hashes(),'Source changed during run'
    return dict(task='T04.2',passed=True,counts=counts,scenario=result,identity=identity,world_checks=checks,metadata_setup_seconds=setup,source_hashes=before,source_unchanged=True,authoritative_source=source,spec_ids=['SPEC07'],invariant_ids=['INV06','INV07','INV08'],limitations=['Only raw SDK outputs scripted; no paid/live model quality claim.','Schedules and payment records stay separate; no derived fully-paid conclusion or money movement.','Human review and independent browser coverage not implied by native gate.'])

def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('output',type=Path);parser.add_argument('--task',choices=['T04.2'],default='T04.2');args=parser.parse_args();folder=args.output.resolve()
    if not folder.is_relative_to((ROOT/'evidence/scenarios').resolve()):raise SystemExit('Evidence path outside scenarios')
    folder.mkdir(parents=True,exist_ok=False);os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP','True')
    try:r=asyncio.run(execute(folder))
    except BaseException as e:
        (folder/'report.json').write_text(json.dumps(dict(task='T04.2',passed=False,error_type=type(e).__name__,claim='Failed attempt retained, not passing'),indent=2));raise
    (folder/'report.json').write_text(json.dumps(r,indent=2));(folder/'manifest.json').write_text(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()},indent=2));print(json.dumps(dict(task='T04.2',passed=True,pytest=r['counts'],scenarios=r['scenario']['accepted_scenarios'])))
if __name__=='__main__':main()
