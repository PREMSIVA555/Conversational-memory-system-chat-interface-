"""T03.3 real socket HTTP, PostgreSQL16 and Redis7; raw model transport only is scripted."""
import asyncio
import json
import socket
import subprocess
import sys
import os
from dataclasses import asdict
from dataclasses import replace
from uuid import uuid4
import httpx
import psycopg
import pytest
import pytest_asyncio
import uvicorn
from api.foundation import create_app
from store.banking_migrate import apply
from tests.p03.test_coordinator import ScriptedTransport,plan

@pytest.fixture(autouse=True)
def migrate_turns(config):
    with psycopg.connect(config['dsns']['migrator']) as c:apply(c,config['roles'],through='0024')

@pytest_asyncio.fixture
async def server(settings,world):
    transport=ScriptedTransport(plan(world['accounts'][0]))
    app=create_app(settings,model_transport=transport,turns_enabled=True,turn_worker_paused=True)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='critical',access_log=False))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(200):
        if server.started:break
        await asyncio.sleep(.01)
    assert server.started
    clients=[]
    for owner in world['owners']:
        token=await app.state.sessions.issue(owner);context=await app.state.sessions.require(token)
        client=httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',headers={'Cookie':'__Host-bank_sid='+token,'Origin':settings.origin,'X-CSRF-Token':context.csrf},timeout=10)
        clients.append((client,context))
    yield app,clients,transport
    for client,context in clients:
        await client.aclose();await app.state.sessions.revoke(context)
    server.should_exit=True
    completed,_=await asyncio.wait([task],timeout=8)
    if not completed:
        for pending in asyncio.all_tasks():
            print('SHUTDOWN TASK',pending.get_coro().__qualname__)
            pending.print_stack(limit=4)
        server.force_exit=True
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        raise AssertionError('Actual HTTP server failed bounded shutdown')

async def conversation(client):
    r=await client.post('/api/v1/conversations',json={});assert r.status_code==201,r.text
    return '/api/v1/conversations/'+r.json()['id']+'/turns'
async def accept(client,base,text,**fields):
    body=dict(client_turn_id=str(uuid4()),text=text,**fields)
    r=await client.post(base,json=body);assert r.status_code==202,r.text
    return body,r.json()
async def terminal(client,turn):
    for _ in range(200):
        r=await client.get(turn['status_url']);assert r.status_code==200,r.text
        if r.json()['result'] is not None:return r.json()
        await asyncio.sleep(.025)
    raise AssertionError('Durable worker did not terminate')

@pytest.mark.asyncio
async def test_concurrent_lost_ack_changed_payload_and_owner(server,world,config):
    app,clients,transport=server;c,context=clients[0];foreign=clients[1][0]
    base=await conversation(c);body=dict(client_turn_id=str(uuid4()),text='  Balance '+world['accounts'][0]+'\r\n')
    results=await asyncio.gather(*(c.post(base,json=body) for _ in range(8)))
    assert all(r.status_code==202 for r in results),[r.text for r in results]
    assert len({r.json()['turn_id'] for r in results})==1
    t=results[0].json();recovered=await c.get(base+'/by-client/'+body['client_turn_id'])
    assert recovered.json()['turn_id']==t['turn_id']
    assert (await c.post(base,json={**body,'text':'changed'})).status_code==409
    other=await conversation(c)
    assert (await c.post(other,json=body)).status_code==409
    assert (await foreign.get(t['status_url'])).status_code==404
    assert (await foreign.get(t['events_url'])).status_code==404
    assert (await c.post(base,json={**body,'customer_id':context.customer_id})).status_code==422
    with psycopg.connect(config['admin_dsn']) as sql:
        row=sql.execute('SELECT t.run_id,t.source_id,t.outbox_id,r.deadline,r.started_at FROM banking.turns t JOIN banking.account_runs r ON r.id=t.run_id WHERE t.id=%s',(t['turn_id'],)).fetchone()
        assert row and (row[3]-row[4]).total_seconds()<=10.1
        assert sql.execute('SELECT count(*) FROM banking.accepted_user_events WHERE customer_id=%s AND client_event_id=%s',(context.customer_id,body['client_turn_id'])).fetchone()[0]==1
    app.state.turn_worker.paused=False
    done=await terminal(c,t);assert done['status']=='completed' and '90071992547409.93' in done['result']['text']
    assert len(transport.calls)==1
    events=await c.get(t['events_url']);assert events.status_code==200
    assert events.text.count('event: answer.ready')==1
    assert [line for line in events.text.splitlines() if line.startswith('id:')]==['id: 1','id: 2','id: 3']
    reconnect=await c.get(t['events_url'],headers={'Last-Event-ID':'2'})
    assert 'id: 3' in reconnect.text and 'id: 1' not in reconnect.text
    assert (await c.get(t['events_url'],headers={'Last-Event-ID':'999'})).status_code==422

@pytest.mark.asyncio
async def test_queue_deadline_not_reset_and_session_expired(server,world,config):
    app,clients,transport=server;c,context=clients[0];base=await conversation(c)
    _,t=await accept(c,base,'Balance '+world['accounts'][0])
    with psycopg.connect(config['admin_dsn']) as sql:
        sql.execute("UPDATE banking.account_runs SET started_at=clock_timestamp()-interval '30 seconds',deadline=clock_timestamp()-interval '1 second' WHERE id=(SELECT run_id FROM banking.turns WHERE id=%s)",(t['turn_id'],))
    app.state.turn_worker.paused=False
    done=await terminal(c,t);assert done['status']=='expired' and transport.calls==[]
    app.state.turn_worker.paused=True
    _,second=await accept(c,base,'Second balance '+world['accounts'][0])
    await app.state.sessions.revoke(context)
    app.state.turn_worker.paused=False
    await asyncio.sleep(.3)
    with psycopg.connect(config['admin_dsn']) as sql:
        row=sql.execute('SELECT status,result FROM banking.turns WHERE id=%s',(second['turn_id'],)).fetchone()
        assert row[0]=='session_expired' and not row[1]['evidence']
    assert (await c.get(second['status_url'])).status_code==401
    assert (await c.get(second['events_url'])).status_code==401

@pytest.mark.asyncio
async def test_erasure_pending_blocks_source_execution_and_read(server,world,config):
    app,clients,transport=server;c,context=clients[0];base=await conversation(c)
    _,t=await accept(c,base,'Balance '+world['accounts'][0])
    async with app.state.database.session(context) as sql:
        row=await (await sql.execute('SELECT source_id FROM banking.turns WHERE id=%s',(t['turn_id'],))).fetchone()
        await sql.execute('SELECT banking.erase_memory_source(%s)',(row['source_id'],))
    app.state.turn_worker.paused=False
    await asyncio.sleep(.3)
    assert (await c.get(t['status_url'])).status_code==404
    assert (await c.get(t['events_url'])).status_code==404
    assert (await c.get(base)).json()['items']==[]
    assert transport.calls==[]

@pytest.mark.asyncio
async def test_selection_version_instructions_and_no_global_capture(server,world):
    app,clients,transport=server;c,context=clients[0];base=await conversation(c)
    async with app.state.database.session(context) as sql:
        row=await (await sql.execute('SELECT version FROM banking.accounts WHERE id=%s',(world['accounts'][0],))).fetchone()
    _,t=await accept(c,base,'Balance',selection={'account_id':world['accounts'][0],'version':str(row['version'])},instructions={'language':'hi','format':'detailed'})
    app.state.turn_worker.paused=False
    done=await terminal(c,t)
    assert done['status']=='completed' and 'वर्तमान शेष' in done['result']['text'] and 'स्रोत समय' in done['result']['text']
    from store.banking_preferences import Preferences
    pref=await Preferences(app.state.database).read(context)
    assert pref['effective']['preferred_response_language']['value']=='en'

@pytest.mark.asyncio
async def test_atomic_rollback_and_consumer_does_not_claim(server,config,world):
    app,clients,transport=server;c,context=clients[0];base=await conversation(c)
    _,t=await accept(c,base,'Balance '+world['accounts'][0])
    from jobs.banking_consumer import Consumer
    with psycopg.connect(config['admin_dsn']) as sql:outbox=sql.execute('SELECT outbox_id FROM banking.turns WHERE id=%s',(t['turn_id'],)).fetchone()[0]
    consumer=Consumer(config['dsns']['dispatcher'],config['dsns']['worker'])
    assert await asyncio.to_thread(consumer.claim,event_id=outbox)==[]
    key=str(uuid4())
    async with app.state.database.session(context) as sql:
        before=(await (await sql.execute('SELECT count(*) AS n FROM banking.accepted_user_events')).fetchone())['n']
    with pytest.raises(psycopg.Error):
        async with app.state.database.session(context) as sql:
            await sql.execute("SELECT banking.accept_turn(%s,%s,%s::jsonb,%s,%s,%s,10)",(key,base.split('/')[4],json.dumps({'text':'rollback','run_fingerprint':'bad'}),'f'*64,context.session_key,'a'*64))
            await sql.execute('SELECT 1/0')
    async with app.state.database.session(context) as sql:
        after=(await (await sql.execute('SELECT count(*) AS n FROM banking.accepted_user_events')).fetchone())['n']
    assert after==before

@pytest.mark.asyncio
async def test_real_process_crash_claim_recovery_preserves_budget(server,settings,config,world,tmp_path):
    app,clients,transport=server;c,context=clients[0];base=await conversation(c)
    app.state.turns.run_seconds=30
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    control=tmp_path/'process.private.json'
    process=None
    def launch(delay,prepare_metadata=False):
        control.write_text(json.dumps(dict(settings=asdict(settings),plan=plan(world['accounts'][0]),port=port,delay=delay,prepare_metadata=prepare_metadata)),encoding='utf-8')
        return subprocess.Popen([sys.executable,'-m','tests.p03.turn_process',str(control)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env={**os.environ,'LITELLM_LOCAL_MODEL_COST_MAP':'True'})
    try:
        process=launch(15,prepare_metadata=True)
        # A serving initial worker exists before accepting user work. Only its
        # setup is excluded; killing it and starting a cold replacement must
        # still consume the unchanged 30-second accepted-run budget.
        async with httpx.AsyncClient(timeout=.5) as probe:
            for _ in range(600):
                assert process.poll() is None,'Initial private worker exited during setup'
                try:
                    if (await probe.get(f'http://127.0.0.1:{port}/health')).status_code==200:break
                except httpx.TransportError:pass
                await asyncio.sleep(.05)
            else:raise AssertionError('Initial private worker did not become ready')
        _,t=await accept(c,base,'Process restart balance '+world['accounts'][0])
        original=None
        for _ in range(400):
            with psycopg.connect(config['admin_dsn']) as sql:
                row=sql.execute('SELECT r.deadline,r.model_calls,t.attempts FROM banking.account_runs r JOIN banking.turns t ON t.run_id=r.id WHERE t.id=%s',(t['turn_id'],)).fetchone()
            original=row[0]
            if row[1]==1:break
            assert process.poll() is None,'Actual private API process unexpectedly exited'
            await asyncio.sleep(.025)
        assert row[1]==1
        process.kill();await asyncio.to_thread(process.wait,5)
        process=launch(0)
        done=await terminal(c,t)
        assert done['status']=='completed',done
        with psycopg.connect(config['admin_dsn']) as sql:
            after=sql.execute('SELECT r.deadline,r.model_calls,t.attempts FROM banking.account_runs r JOIN banking.turns t ON t.run_id=r.id WHERE t.id=%s',(t['turn_id'],)).fetchone()
        assert after[0]==original and after[1]==2 and after[2]==2
    finally:
        if process and process.poll() is None:process.kill();await asyncio.to_thread(process.wait,5)

@pytest.mark.asyncio
@pytest.mark.parametrize('expire',[False,True])
async def test_active_stream_stops_on_real_session_revocation_or_expiry(server,world,expire):
    app,clients,transport=server;c,context=clients[0];base=await conversation(c)
    transport.delay=.8
    _,t=await accept(c,base,'Balance '+world['accounts'][0])
    app.state.turn_worker.paused=False
    async with c.stream('GET',t['events_url']) as stream:
        assert stream.status_code==200
        lines=stream.aiter_lines()
        first=[]
        async for line in lines:
            first.append(line)
            if line=='':break
        if expire:await app.state.sessions.command('pexpire',context.session_key,30)
        else:await app.state.sessions.revoke(context)
        remaining=[line async for line in lines]
    assert not any('answer.ready' in line or 'current balance' in line for line in remaining)
    await asyncio.sleep(.9)
    assert (await c.get(t['status_url'])).status_code==401

@pytest.mark.asyncio
async def test_missing_role_context_and_stale_generation_rejected(server,config,world):
    app,clients,transport=server;c,context=clients[0];base=await conversation(c)
    _,t=await accept(c,base,'Balance '+world['accounts'][0])
    for role in ('runtime','worker'):
        with psycopg.connect(config['dsns'][role]) as sql:
            assert sql.execute('SELECT count(*) FROM banking.turns').fetchone()[0]==0
            assert sql.execute('SELECT count(*) FROM banking.turn_events').fetchone()[0]==0
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(config['dsns']['runtime']) as sql:
            sql.execute("SELECT banking.accept_turn(%s,%s,'{}',%s,'ref',%s,10)",(uuid4(),base.split('/')[4],'a'*64,'b'*64))
    async with app.state.database.worker.connection() as sql:
        first=await (await sql.execute('SELECT * FROM banking.claim_turn(%s)',(app.state.settings.namespace,))).fetchone()
    assert str(first['id'])==t['turn_id']
    with psycopg.connect(config['admin_dsn']) as sql:
        before=sql.execute('SELECT generation FROM banking.account_runs WHERE id=%s',(first['run_id'],)).fetchone()[0]
        sql.execute("UPDATE banking.turns SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(first['id'],))
    async with app.state.database.worker.connection() as sql:
        second=await (await sql.execute('SELECT * FROM banking.claim_turn(%s)',(app.state.settings.namespace,))).fetchone()
    assert second['generation']==first['generation']+1
    with psycopg.connect(config['admin_dsn']) as sql:
        after=sql.execute('SELECT generation FROM banking.account_runs WHERE id=%s',(first['run_id'],)).fetchone()[0]
    assert after==before+1
    assert not await app.state.turn_worker.finish(first,dict(status='unavailable',text='Current bank information is unavailable. Please try again later.',evidence=[]))
    await app.state.turn_worker.execute(second)
    assert (await terminal(c,t))['status']=='completed'

@pytest.mark.asyncio
async def test_same_customer_other_session_cannot_replay_or_read_turn(server,world):
    app,clients,transport=server;c,context=clients[0];base=await conversation(c)
    body,t=await accept(c,base,'Balance '+world['accounts'][0])
    token=await app.state.sessions.issue(context.customer_id)
    second=await app.state.sessions.require(token)
    headers={'Cookie':'__Host-bank_sid='+token,'X-CSRF-Token':second.csrf}
    assert (await c.get(t['status_url'],headers=headers)).status_code==404
    assert (await c.get(t['events_url'],headers=headers)).status_code==404
    assert (await c.get(base,headers=headers)).json()['items']==[]
    assert (await c.post(base,json=body,headers=headers)).status_code==409
    assert (await c.get(t['status_url'])).status_code==200
    await app.state.sessions.revoke(second)

@pytest.mark.asyncio
async def test_replay_uses_frozen_selection_and_feature_off_preserves_recovery(server,world,config):
    app,clients,transport=server;c,context=clients[0];base=await conversation(c)
    async with app.state.database.session(context) as sql:
        version=(await (await sql.execute('SELECT version FROM banking.accounts WHERE id=%s',(world['accounts'][0],))).fetchone())['version']
    body,t=await accept(c,base,'Balance',selection={'account_id':world['accounts'][0],'version':str(version)})
    with psycopg.connect(config['admin_dsn']) as sql:sql.execute('UPDATE banking.accounts SET current_minor=current_minor+1 WHERE id=%s',(world['accounts'][0],))
    replay=await c.post(base,json=body)
    assert replay.status_code==202 and replay.json()['turn_id']==t['turn_id'] and replay.json()['replayed'] is True
    app.state.turn_acceptance_enabled=False
    assert (await c.post(base,json={**body,'client_turn_id':str(uuid4())})).status_code==503
    assert (await c.get(base+'/by-client/'+body['client_turn_id'])).json()['turn_id']==t['turn_id']
    app.state.turn_worker.paused=False
    done=await terminal(c,t)
    assert done['status']=='unavailable' and done['result']['evidence']==[] and transport.calls==[]

@pytest.mark.asyncio
async def test_worker_claim_respects_server_redis_namespace(server,world,settings,config):
    from auth.sessions import Sessions
    from store.banking import Database
    from store.banking_turns import Turns,TurnRequest
    app,clients,transport=server;c,context=clients[0]
    secondsettings=replace(settings,namespace='isolated_'+uuid4().hex)
    sessions=Sessions(secondsettings);database=Database(secondsettings,sessions)
    await database.open()
    token=await sessions.issue(context.customer_id);second=await sessions.require(token)
    try:
        foreignconversation=await database.create_conversation(second)
        foreign=await Turns(database).accept(second,str(foreignconversation['id']),TurnRequest(client_turn_id=str(uuid4()),text='Other isolated server'))
        base=await conversation(c);_,t=await accept(c,base,'Balance '+world['accounts'][0])
        async with app.state.database.worker.connection() as sql:
            claimed=await (await sql.execute('SELECT * FROM banking.claim_turn(%s)',(settings.namespace,))).fetchone()
        assert str(claimed['id'])==t['turn_id']
        with psycopg.connect(config['admin_dsn']) as sql:
            assert sql.execute('SELECT generation FROM banking.turns WHERE id=%s',(foreign['turn_id'],)).fetchone()[0]==0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with psycopg.connect(config['dsns']['worker']) as sql:sql.execute('SELECT * FROM banking.claim_turn()')
    finally:
        await sessions.revoke(second);await database.close();await sessions.close()

@pytest.mark.asyncio
async def test_retry_exhaustion_does_not_loop_when_auth_store_unavailable(server,config,world,monkeypatch):
    """Real acceptance/SQL; explicitly labeled auth-availability wrapper only after accept."""
    from fastapi import HTTPException
    app,clients,transport=server;c,context=clients[0];base=await conversation(c)
    _,t=await accept(c,base,'Balance '+world['accounts'][0])
    with psycopg.connect(config['admin_dsn']) as sql:sql.execute('UPDATE banking.turns SET attempts=3 WHERE id=%s',(t['turn_id'],))
    calls=[]
    async def unavailable(key):calls.append(key);raise HTTPException(503)
    with monkeypatch.context() as patch:
        patch.setattr(app.state.sessions,'require_key',unavailable)
        assert await app.state.turn_worker.one() is True
    done=await terminal(c,t)
    assert done['status']=='unavailable' and done['result']['evidence']==[]
    assert calls==[] and transport.calls==[]
    with psycopg.connect(config['admin_dsn']) as sql:
        assert sql.execute('SELECT attempts FROM banking.turns WHERE id=%s',(t['turn_id'],)).fetchone()[0]==4
