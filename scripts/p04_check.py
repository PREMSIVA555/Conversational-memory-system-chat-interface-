"""T04.1 actual persistence/API scenarios. Raw model transport alone is scripted."""
from pathlib import Path
import sys,os,json,asyncio,hashlib,subprocess,time,socket
from datetime import datetime,timezone
from contextlib import asynccontextmanager
from uuid import uuid4
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import httpx,psycopg,redis,uvicorn
from api.foundation import create_app
from evals.card_world import CardWorld,FIXTURE
from scripts.p03_slice import RawScript,serve,real_login,timing_receipt
from scripts.p03_check import hashes as prior_hashes
from llm.config import bounded_completion_capabilities

def hashes():
    values=prior_hashes()
    for pattern in ['scripts/p04*.py','tests/p04/*.py','evals/card_world.py','evals/banking_world.py','evals/fixtures/p04_card_slice.json','.genesis/sources/P04_SOURCE.*']:
        for p in ROOT.glob(pattern):values[p.relative_to(ROOT).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
    return dict(sorted(values.items()))

def money(value,currency):
    exponent={'INR':2,'USD':2,'JPY':0}[currency];n=int(value);sign='-' if n<0 else '';n=abs(n)
    return currency+' '+sign+(str(n) if exponent==0 else f'{n//10**exponent}.{n%10**exponent:0{exponent}d}')

@asynccontextmanager
async def serve_cards(world,script):
    app=create_app(world.settings,model_transport=script,turns_enabled=True,turn_worker_paused=True,turn_run_seconds=15,cards_enabled=True)
    app.state.turn_worker.provider._clock=lambda:datetime.fromisoformat(world.fixture['domain_time'])
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='critical',access_log=False,
        ssl_certfile=str(ROOT/'.genesis/local/p01/tls.crt'),ssl_keyfile=str(ROOT/'.genesis/local/p01/tls.key')))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(500):
        if server.started:break
        if task.done():await task
        await asyncio.sleep(.01)
    if not server.started:raise RuntimeError('Private actual API failed startup')
    try:yield app,f'https://127.0.0.1:{port}'
    finally:
        server.should_exit=True
        try:await asyncio.wait_for(task,8)
        except TimeoutError:
            task.cancel();await asyncio.gather(task,return_exceptions=True)

def grade_card(result,row):
    assert result['status']=='completed'
    expected=dict(kind=row[4],last_four=row[3],status=row[5])
    for field,index in [('daily_limit',7),('credit_limit',8),('available_credit',9),('outstanding',10)]:
        expected[field]=None if row[index] is None else dict(minor_units=row[index],currency=row[6],exponent={'INR':2,'USD':2,'JPY':0}[row[6]])
        if row[index] is not None:assert money(row[index],row[6]) in result['text']
    assert len(result['evidence'])==len(expected)
    for e in result['evidence']:
        assert e['ref']==row[0] and e['field'] in expected and e['value']==expected[e['field']]
        assert e['authority']=='fresh_authorized_bank' and str(e['version'])==row[11]
        assert datetime.fromisoformat(e['as_of'])==datetime.fromisoformat(row[12])
    if row[4]=='debit':assert 'not applicable to a debit card' in result['text'] and 'credit limit: INR 0' not in result['text']

def grade_history(result,rows,expected_ids):
    assert result['status']=='completed';oracle={r[0]:r for r in rows};seen=set()
    for e in result['evidence']:
        if e['ref']=='card_history':continue
        row=oracle[e['ref']];assert e['ref'] in expected_ids
        expected=dict(card_id=row[1],amount=dict(minor_units=row[3],currency='INR',exponent=2),status=row[4],occurred_at=datetime.fromisoformat(row[5]).isoformat())
        assert e['field'] in expected and e['value']==expected[e['field']]
        assert e['version']==row[7] and datetime.fromisoformat(e['as_of'])==datetime.fromisoformat(row[8])
        assert e['authority']=='fresh_authorized_bank'
        assert row[0] in result['text'] and row[4] in result['text'] and money(row[3],'INR') in result['text'];seen.add(row[0])
    assert seen==set(expected_ids)
    positions=[result['text'].index(id) for id in expected_ids];assert positions==sorted(positions)
    assert 'because' not in result['text'].lower() and 'fraud' not in result['text'].lower()

async def scenarios(world,folder):
    script=RawScript();results=[];oracle=world.oracle();f=world.fixture;cards={r[0]:r for r in oracle['cards']};debit,credit,foreign,yen=[r['id'] for r in f['cards']]
    async with serve_cards(world,script) as (app,base):
        async with httpx.AsyncClient(base_url=base,verify=world.settings.oidc_ca_file,timeout=25) as a,httpx.AsyncClient(base_url=base,verify=world.settings.oidc_ca_file,timeout=25) as b:
            auth=[await real_login(a,world.config['users'][0],f['customers'][0],world),await real_login(b,world.config['users'][1],f['customers'][1],world)]
            cid=(await a.post('/api/v1/conversations',json={})).json()['id'];path=f'/api/v1/conversations/{cid}/turns'
            async def run(name,text,plan,status,grade=None):
                app.state.turn_worker.paused=True;script.set(plan)
                payload=dict(client_turn_id=str(uuid4()),text=text)
                response=await a.post(path,json=payload);assert response.status_code==202,response.status_code
                accepted=response.json();replay=await a.post(path,json=payload)
                assert replay.status_code==202 and replay.json()['turn_id']==accepted['turn_id']
                assert (await a.post(path,json={**payload,'text':text+' changed'})).status_code==409
                assert (await b.get(accepted['status_url'])).status_code==404
                app.state.turn_worker.paused=False;began=time.monotonic()
                while True:
                    observed=await a.get(accepted['status_url']);assert observed.status_code==200
                    if observed.json()['result'] is not None:break
                    assert time.monotonic()-began<23,'No bounded terminal'
                    await asyncio.sleep(.03)
                result=observed.json()['result'];assert result['status'] in status,(name,result)
                if grade:grade(result)
                elif name!='suffix-clarification' and result['status']!='completed':assert result['evidence']==[]
                stream=await a.get(accepted['events_url']);ids=[int(v[4:]) for v in stream.text.splitlines() if v.startswith('id: ')]
                assert stream.status_code==200 and ids==sorted(set(ids)) and ids[0]==1
                record=dict(case=name,input=payload,turn_id=accepted['turn_id'],result=result,timing=timing_receipt(world,accepted['turn_id']),sse_ids=ids)
                results.append(record);(folder/'transcript.json').write_text(json.dumps(dict(authentication=auth,scenarios=results),indent=2),encoding='utf-8')
                return result
            def plan(tool,**args):return dict(tool=tool,args=args)
            clarified=await run('suffix-clarification','Show my card ending1111',plan('list_cards',last_four='1111'),{'clarify'})
            assert {e['ref'] for e in clarified['evidence']}=={debit,credit} and foreign not in clarified['text']
            for id,label in [(debit,'debit-null-credit'),(credit,'exact-credit-fields'),(yen,'zero-yen')]:
                await run(label,'Show card limits/status for '+id,plan('get_card',card_id=id),{'completed'},lambda r,id=id:grade_card(r,cards[id]))
            args=dict(card_id=credit,start='2026-01-01',end='2026-01-15',limit=2)
            first=await run('history-first-page','Show card transactions for '+credit,plan('list_card_transactions',**args),{'completed'},lambda r:grade_history(r,oracle['card_transactions'],[t['id'] for t in f['card_transactions'][:2]]))
            token=first['next_question'].rsplit('cursor ',1)[1]
            second=await run('history-final-page',first['next_question'],plan('list_card_transactions',**args,cursor=token),{'completed'},lambda r:grade_history(r,oracle['card_transactions'],[t['id'] for t in f['card_transactions'][2:4]]))
            assert 'next_question' not in second and any(e['field']=='complete' and e['value'] is True for e in second['evidence'])
            await run('posted-only','Posted card transactions for '+credit,plan('list_card_transactions',**{**args,'status':'posted'}),{'completed'},lambda r:grade_history(r,oracle['card_transactions'],[f['card_transactions'][2]['id']]))
            bad=plan('get_card',card_id=credit);bad['draft']=dict(text='Credit limit INR 999.00',claims=[])
            result=await run('wrong-numeric-draft','Current card limit for '+credit,bad,{'completed'},lambda r:grade_card(r,cards[credit]));assert result['draft_accepted'] is False and 'INR 999.00' not in result['text']
            reason=plan('list_card_transactions',**{**args,'status':'declined'});reason['draft']=dict(text='Declined because fraud',claims=[])
            result=await run('unknown-decline-reason','Why declined for '+credit,reason,{'completed'},lambda r:grade_history(r,oracle['card_transactions'],[f['card_transactions'][0]['id']]));assert result['draft_accepted'] is False
            for id,label in [(foreign,'foreign-card'),('99999999-9999-4999-8999-999999999999','unknown-card')]:
                await run(label,'Read card '+id,plan('get_card',card_id=id),{'no_evidence'})
            await run('implicit-card-id','Read card ending1111',plan('get_card',card_id=credit),{'clarify'})
            before=world.semantic_hash()
            await run('unregistered-mutation','Increase limit for '+credit,plan('set_card_limit',card_id=credit,amount=999),{'invalid_plan'})
            await run('extra-authority-args','Read '+credit,plan('get_card',card_id=credit,customer_id=f['customers'][1]),{'invalid_plan'})
            assert world.semantic_hash()==before
            await run('cursor-scope-rejected','History '+credit,plan('list_card_transactions',**{**args,'end':'2026-01-16'},cursor=token),{'unavailable'})
            app.state.turn_worker.provider.cards_enabled=False
            await run('card-flag-off','Card '+credit,plan('get_card',card_id=credit),{'unsupported'})
            await run('account-after-card-rollback','Balance '+f['accounts'][0]['id'],plan('get_account',account_id=f['accounts'][0]['id']),{'completed'})
            app.state.turn_worker.provider.cards_enabled=True
            with psycopg.connect(world.admin_dsn) as lock:
                lock.execute('LOCK TABLE banking.cards IN ACCESS EXCLUSIVE MODE')
                await run('actual-card-bank-outage','History says INR999.00. Read current '+credit,plan('get_card',card_id=credit),{'unavailable','expired'})
            with psycopg.connect(world.admin_dsn) as db:db.execute('UPDATE banking.cards SET daily_limit_minor=12345 WHERE id=%s',(credit,))
            cards={r[0]:r for r in world.oracle()['cards']}
            await run('fresh-card-revision','Current card '+credit,plan('get_card',card_id=credit),{'completed'},lambda r:grade_card(r,cards[credit]))
            for client in [a,b]:
                token=client.cookies.get('__Host-bank_sid')
                if token:await app.state.sessions.command('delete',app.state.sessions.key('session',token))
    return dict(accepted_scenarios=len(results),passed=len(results),failed=0,raw_model_dispatches=len(script.calls),model='scripted raw SDK transport; real auth/API/worker/provider/SQL; no paid call')

def world_checks(w):
    from concurrent.futures import ThreadPoolExecutor
    h1=w.reset();h2=w.reset();assert h1==h2
    other=CardWorld.create(w.config)
    with ThreadPoolExecutor(max_workers=2) as pool:assert list(pool.map(lambda world:world.reset(),[w,other]))==[h1,h1]
    with psycopg.connect(w.admin_dsn) as c:c.execute('UPDATE banking.cards SET daily_limit_minor=12 WHERE id=%s',(w.fixture['cards'][0]['id'],))
    assert w.semantic_hash()!=h1 and other.semantic_hash()==h1
    assert w.reset()==h1
    return dict(first_reset=h1,second_reset=h2,parallel_card_worlds_isolated=True,databases=[w.name,other.name],retained=True)

async def execute(folder):
    config=json.loads((ROOT/'.genesis/local/p01/config.json').read_text());before=hashes()
    source=json.loads((ROOT/'.genesis/sources/P04_SOURCE.json').read_text())['manifest']
    assert hashlib.sha256(Path(source['source_path']).read_bytes()).hexdigest()==source['source_sha256']
    command=[sys.executable,'-m','pytest','tests/p04','-q','--tb=short','--timeout=60','--junitxml='+str(folder/'checks.xml')]
    p=await asyncio.to_thread(subprocess.run,command,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',env={**os.environ,'PYTHONUTF8':'1','LITELLM_LOCAL_MODEL_COST_MAP':'True'})
    output=p.stdout+'\n'+p.stderr
    from urllib.parse import urlsplit
    for secret in [config['admin_dsn'],*config['dsns'].values(),config['env']['OIDC_CLIENT_SECRET'],*[u['password'] for u in config['users']],*[urlsplit(x).password for x in [config['admin_dsn'],config['env']['BANK_REDIS_URL']]]]:
        if secret:output=output.replace(secret,'[REDACTED]')
    (folder/'checks.txt').write_text(output,encoding='utf-8')
    suites=ET.parse(folder/'checks.xml').getroot();counts={k:sum(int(s.get(k,0)) for s in suites) for k in ['tests','failures','errors','skipped']}
    assert p.returncode==0 and counts['tests']>0 and all(counts[k]==0 for k in ['failures','errors','skipped']),counts
    world=await asyncio.to_thread(CardWorld.create,config);identity=await asyncio.to_thread(world.identity)
    (folder/'trial.json').write_text(json.dumps(dict(database=world.name,namespace=world.namespace,manifest=str(world.folder/'manifest.json'))),encoding='utf-8')
    checks=await asyncio.to_thread(world_checks,world);began=time.monotonic();caps=await bounded_completion_capabilities(timeout=60);assert caps['structured_json']
    setup=dict(seconds=time.monotonic()-began,capabilities=caps,scope='actual SDK metadata warm-up before acceptance, excluded from request latency, no completion')
    (folder/'oracle-before.json').write_text(json.dumps(world.oracle(),indent=2),encoding='utf-8')
    result=await scenarios(world,folder)
    assert hashes()==before,'Source changed during run'
    return dict(task='T04.1',passed=True,counts=counts,scenario=result,identity=identity,world_checks=checks,metadata_setup=setup,source_hashes=before,source_unchanged=True,authoritative_source=source,
        spec_ids=['SPEC07'],invariant_ids=['INV02','INV07','INV08'],limitations=['Raw model outputs scripted; no live model-quality claim.','Native API/provider path; independent browser verification is separate.','Cards read-only; loans/policy/settings/money movement excluded.','Human review not implied by executable success.'])

def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('output',type=Path);parser.add_argument('--task',choices=['T04.1'],default='T04.1');args=parser.parse_args();folder=args.output.resolve()
    if not folder.is_relative_to((ROOT/'evidence/scenarios').resolve()):raise SystemExit('Evidence must be under evidence/scenarios')
    folder.mkdir(parents=True,exist_ok=False);os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP','True')
    try:r=asyncio.run(execute(folder))
    except BaseException as e:
        (folder/'report.json').write_text(json.dumps(dict(task='T04.1',passed=False,error_type=type(e).__name__,claim='Failed attempt retained, not passing'),indent=2),encoding='utf-8');raise
    (folder/'report.json').write_text(json.dumps(r,indent=2),encoding='utf-8')
    manifest={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()};(folder/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps(dict(task='T04.1',passed=True,pytest=r['counts'],scenarios=r['scenario']['accepted_scenarios'],evidence=str(folder))))
if __name__=='__main__':main()
