"""Actual HTTP and Docker SQL/Redis; only the raw application SDK is scripted."""
import asyncio,json,socket
from pathlib import Path
from uuid import uuid4
import httpx,psycopg,pytest,pytest_asyncio,uvicorn
from api.foundation import create_app
from evals.workspace_world import WorkspaceWorld
from store.banking_preferences import binding
from graphs.workspace import validate_wording
from tests.p03.test_coordinator import ScriptedTransport

@pytest.fixture(scope='module')
def world():
    w=WorkspaceWorld.create(json.loads(Path('.genesis/local/p01/config.json').read_text(encoding='utf-8')))
    assert w.identity()['matched'];return w

@pytest_asyncio.fixture
async def service(world):
    transport=ScriptedTransport({'tool':'clarify','args':{}})
    app=create_app(world.settings,turns_enabled=True,workspace_enabled=True,cards_enabled=True,loans_enabled=True,policies_enabled=True,turn_run_seconds=15,model_transport=transport)
    sock=socket.socket();sock.bind(('127.0.0.1',0));url=f'http://127.0.0.1:{sock.getsockname()[1]}'
    server=uvicorn.Server(uvicorn.Config(app,log_level='critical',access_log=False));task=asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(500):
        if server.started:break
        await asyncio.sleep(.01)
    assert server.started
    clients=[];contexts=[]
    for owner in [0,0,1]:
        token=await app.state.sessions.issue(world.fixture['customers'][owner]);ctx=await app.state.sessions.require(token);contexts.append(ctx)
        clients.append(httpx.AsyncClient(base_url=url,headers={'Cookie':'__Host-bank_sid='+token,'Origin':world.settings.origin,'X-CSRF-Token':ctx.csrf},timeout=20))
    yield app,clients,contexts,transport,world
    for c,ctx in zip(clients,contexts):await c.aclose();await app.state.sessions.revoke(ctx)
    server.should_exit=True;await asyncio.wait_for(task,10)

async def prepare(c):
    r=await c.post('/api/v1/workspace/prepare',json={});assert r.status_code==200,r.text
    return r.json()['conversation_id']

async def send(c,cid,text,**kw):
    r=await c.post(f'/api/v1/conversations/{cid}/messages',json=dict(client_turn_id=str(uuid4()),text=text,workspace=True,**kw))
    assert r.status_code==202,r.text
    if r.json()['kind']=='preference':return r.json()
    turn=r.json()
    for _ in range(300):
        r=await c.get(turn['status_url']);assert r.status_code==200,r.text
        if r.json()['result'] is not None:return r.json()['result']
        await asyncio.sleep(.03)
    raise AssertionError('Worker did not publish a terminal result')

def selection(item,kind):return dict(kind=kind,id=item['id'],version=item['source']['version'])

@pytest.mark.asyncio
async def test_prepare_concurrency_greeting_and_owned_overview(service):
    app,cs,ctxs,tr,w=service;c=cs[0]
    results=await asyncio.gather(*(c.post('/api/v1/workspace/prepare',json={}) for _ in range(6)))
    assert all(r.status_code==200 for r in results)
    assert len({r.json()['conversation_id'] for r in results})==1
    cid=results[0].json()['conversation_id'];before=len(tr.calls)
    r=await send(c,cid,'Hi');assert r['message_kind']=='greeting' and r['actions'] and 'additional information' not in r['text']
    assert len(tr.calls)==before
    for kind,expected in [('account',2),('card',3),('loan',3)]:
        page=(await c.get('/api/v1/products/'+kind)).json();assert page['status']=='ready' and len(page['items'])==expected
        assert all(x['id'] not in [y['id'] for y in w.fixture[kind+'s'] if y['owner']==1] for x in page['items'])

@pytest.mark.asyncio
async def test_choices_selection_and_followup(service):
    app,cs,ctxs,tr,w=service;c=cs[0];cid=await prepare(c)
    result=await send(c,cid,'What is my balance?');assert result['status']=='clarify' and len(result['actions'])==2
    chosen=result['actions'][0]['resource_selection']
    answer=await send(c,cid,'View balance',action='balance',resource_selection=chosen)
    assert answer['blocks'][0]['kind']=='account'
    history=await send(c,cid,'Show its transactions')
    assert history['blocks'][0]['kind']=='transactions'
    assert history['workspace']['resource']['id']==chosen['id']
    assert len(tr.calls)==0

@pytest.mark.asyncio
async def test_card_ambiguity_versions_and_foreign_selection(service):
    app,cs,ctxs,tr,w=service;c=cs[0];cid=await prepare(c)
    answer=await send(c,cid,'List my cards ending 1111')
    assert len(answer['actions'])==2 and answer['status']=='clarify',answer
    page=(await cs[2].get('/api/v1/products/card')).json();foreign=selection(page['items'][0],'card')
    r=await c.post(f'/api/v1/conversations/{cid}/messages',json=dict(client_turn_id=str(uuid4()),text='Limits',resource_selection=foreign));assert r.status_code==404
    own=(await c.get('/api/v1/products/card')).json()['items'][0]
    bad=selection(own,'card');bad['version']='999999'
    r=await c.post(f'/api/v1/conversations/{cid}/messages',json=dict(client_turn_id=str(uuid4()),text='Limits',resource_selection=bad));assert r.status_code==409

@pytest.mark.asyncio
async def test_ordinal_refresh_and_product_switch(service):
    app,cs,ctxs,tr,w=service;c=cs[0];cid=await prepare(c)
    credit=next(x for x in (await c.get('/api/v1/products/card')).json()['items'] if x['kind']=='credit')
    r=await send(c,cid,'Transactions from 2026-01-14 to 2026-01-15',action='transactions',resource_selection=selection(credit,'card'))
    rows=r['blocks'][0]['data']['items'];assert len(rows)==4
    follow=await send(c,cid,'Show the second transaction');assert follow['blocks'][0]['data']['items'][0]['id']==rows[1]['id']
    switched=await send(c,cid,'Show my loans');assert all(a['resource_selection']['kind']=='loan' for a in switched['actions'])
    assert await app.state.workspace.context(ctxs[0],cid) is None
    assert (await send(c,cid,'Show its transactions'))['status']=='clarify'

@pytest.mark.asyncio
async def test_friendly_name_and_typed_selection_precedence(service):
    app,cs,ctxs,tr,w=service;c=cs[0];cid=await prepare(c)
    r=await send(c,cid,'Show Synthetic yen debit')
    assert r['blocks'][0]['kind']=='card' and r['blocks'][0]['data']['daily_limit']['currency']=='JPY'
    debit=next(x for x in (await c.get('/api/v1/products/card')).json()['items'] if x['display_name']=='Synthetic debit')
    r=await send(c,cid,'Card details',action='card_details',resource_selection=selection(debit,'card'))
    assert r['workspace']['resource']['id']==debit['id']

@pytest.mark.asyncio
async def test_expired_context_and_latest_fresh_balance(service):
    app,cs,ctxs,tr,w=service;c=cs[0];cid=await prepare(c)
    account=(await c.get('/api/v1/products/account')).json()['items'][0]
    await send(c,cid,'Balance',action='balance',resource_selection=selection(account,'account'))
    with psycopg.connect(w.admin_dsn) as sql:
        sql.execute('UPDATE banking.accounts SET available_minor=available_minor+7 WHERE id=%s',(account['id'],))
    fresh=await send(c,cid,'What is its balance?')
    assert fresh['blocks'][0]['data']['available_balance']['minor_units']==str(int(account['available_balance']['minor_units'])+7)
    with psycopg.connect(w.admin_dsn) as sql:
        sql.execute("UPDATE banking.resource_context SET expires_at=clock_timestamp()-interval '1 second' WHERE session_binding=%s",(binding(ctxs[0]),))
        sql.execute('UPDATE banking.accounts SET available_minor=available_minor-7 WHERE id=%s',(account['id'],))
    assert await app.state.workspace.context(ctxs[0],cid) is None
    assert (await send(c,cid,'Show its transactions'))['status']=='clarify'

@pytest.mark.asyncio
async def test_worker_revalidates_after_durable_acceptance_and_replay(service):
    app,cs,ctxs,tr,w=service;c=cs[0];cid=await prepare(c)
    app.state.turn_worker.paused=True
    await asyncio.sleep(.2)
    account=(await c.get('/api/v1/products/account')).json()['items'][0]
    body=dict(client_turn_id=str(uuid4()),text='Balance',workspace=True,action='balance',resource_selection=selection(account,'account'))
    first=await c.post(f'/api/v1/conversations/{cid}/messages',json=body);assert first.status_code==202
    replay=await c.post(f'/api/v1/conversations/{cid}/messages',json=body)
    assert replay.json()['turn_id']==first.json()['turn_id'] and replay.json()['replayed']
    with psycopg.connect(w.admin_dsn) as sql:sql.execute('UPDATE banking.accounts SET version=version+1 WHERE id=%s',(account['id'],))
    app.state.turn_worker.paused=False
    for _ in range(300):
        result=(await c.get(first.json()['status_url'])).json()['result']
        if result is not None:break
        await asyncio.sleep(.03)
    assert result['status']=='unavailable' and not result['blocks'] and not tr.calls

@pytest.mark.asyncio
async def test_optional_wording_rejects_claims_without_losing_facts(service):
    app,cs,ctxs,tr,w=service;c=cs[0];cid=await prepare(c)
    account=(await c.get('/api/v1/products/account')).json()['items'][0]
    for raw in [{'intro':'Your balance is INR 999.00','follow_up':None},RuntimeError('SDK unavailable'),{'intro':'Here’s what I found.','follow_up':'transactions'}]:
        tr.outputs=[raw]
        r=await send(c,cid,'Explain this balance',action='balance',resource_selection=selection(account,'account'))
        assert r['status']=='completed' and r['blocks'][0]['data']['available_balance']==account['available_balance']
        assert r['wording_status']==('checked' if isinstance(raw,dict) and raw['intro']=='Here’s what I found.' else 'fallback')

@pytest.mark.asyncio
async def test_zero_single_pagination_and_duplicate_product_names(service):
    app,cs,ctxs,tr,w=service
    owner=str(uuid4())
    with psycopg.connect(w.admin_dsn) as sql:sql.execute('INSERT INTO banking.customers(customer_id) VALUES(%s)',(owner,))
    token=await app.state.sessions.issue(owner);ctx=await app.state.sessions.require(token)
    async with httpx.AsyncClient(base_url=cs[0].base_url,headers={'Cookie':'__Host-bank_sid='+token,'Origin':w.settings.origin,'X-CSRF-Token':ctx.csrf}) as c:
        cid=await prepare(c)
        empty=(await c.get('/api/v1/products/account')).json()
        assert empty['status']=='ready' and empty['items']==[] and empty['complete']
        assert (await send(c,cid,'My balance'))['status']=='no_evidence'
        with psycopg.connect(w.admin_dsn) as sql:
            sql.execute("INSERT INTO banking.accounts(id,customer_id,display_name,currency,current_minor,available_minor) VALUES('00000000-0000-4000-8000-000000000001',%s,'Everyday savings','INR',100,90)",(owner,))
        one=await send(c,cid,'My balance');assert one['status']=='completed' and one['workspace']['resource']['label']=='Everyday savings'
        with psycopg.connect(w.admin_dsn) as sql:
            for i in range(20):sql.execute("INSERT INTO banking.accounts(id,customer_id,display_name,currency,current_minor,available_minor) VALUES(%s,%s,%s,'INR',100,90)",(f'00000000-0000-4000-8000-{i+2:012d}',owner,'Everyday savings' if i==0 else f'Extra account {i}'))
        page=(await c.get('/api/v1/products/account')).json();assert len(page['items'])==20 and not page['complete'] and page['next_cursor']
        next_page=(await c.get('/api/v1/products/account',params={'cursor':page['next_cursor']})).json()
        assert len(next_page['items'])==1 and next_page['complete']
        assert len({x['id'] for x in page['items']+next_page['items']})==21
        ambiguous=await send(c,cid,'Show Everyday savings account')
        assert ambiguous['status']=='clarify' and len(ambiguous['actions'])==2
        assert await app.state.workspace.context(ctx,cid) is None
    await app.state.sessions.revoke(ctx)

@pytest.mark.asyncio
async def test_legacy_turn_shape_and_model_outage_are_terminal(service):
    app,cs,ctxs,tr,w=service;c=cs[0];cid=await prepare(c)
    account=(await c.get('/api/v1/products/account')).json()['items'][0]
    tr.outputs=[{'tool':'get_account','args':{'account_id':account['id']}}]
    body=dict(client_turn_id=str(uuid4()),text='Balance',selection={'account_id':account['id'],'version':account['source']['version']})
    response=await c.post(f'/api/v1/conversations/{cid}/turns',json=body);assert response.status_code==202
    for _ in range(300):
        legacy=(await c.get(response.json()['status_url'])).json()['result']
        if legacy is not None:break
        await asyncio.sleep(.03)
    assert legacy['status']=='completed' and legacy['evidence']
    tr.outputs=[RuntimeError('SDK unavailable')]
    unavailable=await send(c,cid,'Can you interpret an unusual enquiry?')
    assert unavailable['status']=='unavailable' and not unavailable['evidence']
    with psycopg.connect(w.admin_dsn) as sql:
        attempts=sql.execute('''SELECT count(*) FROM banking.account_attempts a JOIN banking.turns t ON a.run_id=t.run_id
            WHERE t.conversation_id=%s AND t.request->>'workspace'='true' AND a.kind='model' ''',(cid,)).fetchone()[0]
    assert attempts==1

@pytest.mark.asyncio
async def test_twenty_reference_boundary_and_history_pagination(service):
    app,cs,ctxs,tr,w=service;c=cs[0];cid=await prepare(c)
    account=(await c.get('/api/v1/products/account')).json()['items'][0]
    ids=[str(uuid4()) for _ in range(21)]
    try:
        with psycopg.connect(w.admin_dsn) as sql:
            for i,ref in enumerate(ids):sql.execute('''INSERT INTO banking.account_transactions
                (id,account_id,customer_id,amount_minor,status,booked_on) VALUES(%s,%s,%s,%s,'posted','2026-01-02')''',
                (ref,account['id'],ctxs[0].customer_id,i))
        account=(await c.get('/api/v1/products/account')).json()['items'][0]
        r=await send(c,cid,'Transactions from 2026-01-02 to 2026-01-02; page size 21',action='transactions',resource_selection=selection(account,'account'))
        assert len(r['blocks'][0]['data']['items'])==20 and len(r['workspace']['refs'])==20 and r['next_question']
        ctx=await app.state.workspace.context(ctxs[0],cid);assert len(ctx['refs'])==20
        with psycopg.connect(w.admin_dsn) as sql:
            from psycopg.types.json import Jsonb
            with pytest.raises(psycopg.errors.CheckViolation):
                with sql.transaction():sql.execute('UPDATE banking.resource_context SET refs=%s WHERE session_binding=%s',(Jsonb(ctx['refs']+[ctx['refs'][0]]),binding(ctxs[0])))
        next_page=await send(c,cid,r['next_question'])
        assert len(next_page['blocks'][0]['data']['items'])==1
        assert next_page['blocks'][0]['data']['items'][0]['id'] not in {x['id'] for x in r['blocks'][0]['data']['items']}
    finally:
        with psycopg.connect(w.admin_dsn) as sql:sql.execute('DELETE FROM banking.account_transactions WHERE id=ANY(%s::uuid[])',(ids,))

@pytest.mark.asyncio
async def test_context_is_separate_from_greeting_preferences_and_single_fact_read(service):
    app,cs,ctxs,tr,w=service;c=cs[0];cid=await prepare(c)
    credit=next(x for x in (await c.get('/api/v1/products/card')).json()['items'] if x['kind']=='credit')
    history=await send(c,cid,'Transactions from 2026-01-14 to 2026-01-15',action='transactions',resource_selection=selection(credit,'card'))
    expected=history['blocks'][0]['data']['items'][1]['id']
    for text in ['Hi','Help','Reply in Hindi','Show its card limits','Show its balance']:
        r=await send(c,cid,text)
        context=await app.state.workspace.context(ctxs[0],cid)
        assert context['resource']['id']==credit['id'] and len(context['refs'])==4,(text,r)
    await send(c,cid,'Card details',action='card_details',resource_selection=selection(credit,'card'))
    assert len((await app.state.workspace.context(ctxs[0],cid))['refs'])==4
    second=await send(c,cid,'Show the second transaction')
    assert second['blocks'][0]['data']['items'][0]['id']==expected
    assert len(tr.calls)==0

@pytest.mark.asyncio
async def test_logout_revokes_session_even_when_context_database_cleanup_fails(service,monkeypatch):
    from contextlib import asynccontextmanager
    app,cs,ctxs,tr,w=service;c=cs[0];cid=await prepare(c)
    account=(await c.get('/api/v1/products/account')).json()['items'][0]
    await send(c,cid,'Balance',action='balance',resource_selection=selection(account,'account'))
    normal=app.state.database.session
    @asynccontextmanager
    async def outage(*args,**kwargs):
        raise ConnectionError('Synthetic bank DB cleanup outage')
        yield
    monkeypatch.setattr(app.state.database,'session',outage)
    result=await c.post('/api/v1/auth/logout',json={})
    assert result.status_code==200
    assert (await c.get('/api/v1/auth/session')).status_code==401
    monkeypatch.setattr(app.state.database,'session',normal)
    assert await app.state.workspace.context(ctxs[1],cid) is None

@pytest.mark.asyncio
async def test_session_expiry_and_erasure_context(service):
    app,cs,ctxs,tr,w=service;c=cs[0];cid=await prepare(c)
    account=(await c.get('/api/v1/products/account')).json()['items'][0]
    await send(c,cid,'Balance',action='balance',resource_selection=selection(account,'account'))
    assert await app.state.workspace.context(ctxs[0],cid)
    assert await app.state.workspace.context(ctxs[1],cid) is None
    with psycopg.connect(w.admin_dsn) as sql:
        source=sql.execute('SELECT source_id FROM banking.resource_context WHERE session_binding=%s',(binding(ctxs[0]),)).fetchone()[0]
    r=await c.delete('/api/v1/memory/sources/'+str(source));assert r.status_code==200,r.text
    assert await app.state.workspace.context(ctxs[0],cid) is None
    r=await send(c,cid,'Show its transactions');assert r['status']=='clarify'
    await app.state.sessions.revoke(ctxs[0]);assert (await c.get('/api/v1/products/account')).status_code==401

@pytest.mark.asyncio
async def test_preference_commands_and_csrf(service):
    app,cs,ctxs,tr,w=service;c=cs[0];cid=await prepare(c)
    result=await send(c,cid,'Always reply in Hindi');assert result['kind']=='preference'
    history=(await c.get(f'/api/v1/conversations/{cid}/preference-messages')).json()['items']
    assert history[0]['status']=='persisted'
    hi=await send(c,cid,'Hi');assert 'नमस्ते' in hi['text']
    r=await c.post('/api/v1/workspace/prepare',json={},headers={'X-CSRF-Token':''});assert r.status_code==403
    r=await c.post('/api/v1/workspace/prepare',json={},headers={'Origin':'https://foreign.invalid'});assert r.status_code==403

@pytest.mark.parametrize('value',[{'intro':'Your payment is complete.','follow_up':None},{'intro':'INR 1.00','follow_up':None},{'intro':'Here’s what I found.','follow_up':'transfer'},{'intro':'Here’s what I found.','follow_up':None,'claim':'waived'}])
def test_unsupported_wording_rejected(value):assert validate_wording(json.dumps(value),'en',['transactions']) is None

def test_checked_wording():assert validate_wording(json.dumps({'intro':'Here’s what I found.','follow_up':'transactions'}),'en',['transactions'])

@pytest.mark.asyncio
@pytest.mark.parametrize('question',['Why did my INR2000 card payment yesterday fail?','मेरे कार्ड का भुगतान अस्वीकृत क्यों हुआ?'])
async def test_card_payment_does_not_switch_to_loans(service,question):
    app,cs,ctxs,tr,w=service;c=cs[0];cid=await prepare(c)
    value=await send(c,cid,question)
    assert value['actions']
    assert all(a['resource_selection']['kind']=='card' for a in value['actions'])
    assert all(a['action']=='transactions' for a in value['actions'])

@pytest.mark.asyncio
async def test_unspecified_payment_does_not_guess_loan(service):
    app,cs,ctxs,tr,w=service;c=cs[0];cid=await prepare(c)
    value=await send(c,cid,'Why was my payment declined?')
    assert value['status']=='clarify' and not value['evidence']
    assert not value['actions']
