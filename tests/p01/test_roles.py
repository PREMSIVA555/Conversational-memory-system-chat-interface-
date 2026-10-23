import uuid
import httpx
import pytest
import psycopg
from auth.sessions import SID_COOKIE
from .conftest import login,mutation_headers,observation


async def test_two_customer_api_and_provider_isolation(client,app,settings,config):
    a,_,_=await login(client,config['users'][0])
    created=await client.post('/api/v1/conversations',json={},headers=mutation_headers(settings,a))
    assert created.status_code==201
    resource=created.json()['id']
    assert (await client.get('/api/v1/conversations/'+resource)).status_code==200
    context_a=await app.state.sessions.require(client.cookies.get(SID_COOKIE))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=settings.origin) as other:
        await login(other,config['users'][1])
        context_b=await app.state.sessions.require(other.cookies.get(SID_COOKIE))
        foreign=await other.get('/api/v1/conversations/'+resource)
        absent=await other.get('/api/v1/conversations/'+str(uuid.uuid4()))
        assert foreign.status_code==absent.status_code==404
        assert foreign.json()['detail']==absent.json()['detail']=='Not found'
        assert resource not in str((await other.get('/api/v1/conversations')).json())
        with pytest.raises(Exception) as error: await app.state.database.conversation(context_b,resource)
        assert error.value.status_code==404
    assert str((await app.state.database.conversation(context_a,resource))['id'])==resource


async def test_real_roles_and_pooled_context_reset(app,client,settings,config):
    await login(client,config['users'][0])
    ctx=await app.state.sessions.require(client.cookies.get(SID_COOKIE))
    db=app.state.database
    await db.ready()
    # Force pool reuse with one physical connection for alternating transactions.
    from psycopg_pool import AsyncConnectionPool
    from psycopg.rows import dict_row
    old=db.runtime
    db.runtime=AsyncConnectionPool(settings.runtime_dsn,min_size=1,max_size=1,open=False,kwargs={'row_factory':dict_row})
    await db.runtime.open(wait=True)
    try:
        for _ in range(3):
            async with db.session(ctx) as c:
                assert (await (await c.execute('SELECT count(*) AS n FROM banking.customers')).fetchone())['n']==1
            async with db.runtime.connection() as c:
                assert (await (await c.execute('SELECT count(*) AS n FROM banking.customers')).fetchone())['n']==0
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    async with c.transaction():
                        await c.execute('INSERT INTO banking.conversations(customer_id) VALUES(%s)',(ctx.customer_id,))
    finally:
        await db.runtime.close()
        db.runtime=old
    for kind,dsn in config['dsns'].items():
        with psycopg.connect(dsn) as c:
            flags=c.execute('SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user').fetchone()
            assert flags==(False,False,False,False)
            assert c.execute('SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=current_user)').fetchone()[0]==0


async def test_worker_dispatcher_and_identity_role_grants(config):
    for kind in ['worker','runtime']:
        with psycopg.connect(config['dsns'][kind]) as c:
            assert c.execute('SELECT count(*) FROM banking.customers').fetchone()[0]==0
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute('SELECT * FROM banking.identity_links')
    for kind in ['dispatcher','auth']:
        with psycopg.connect(config['dsns'][kind]) as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute('SELECT * FROM banking.conversations')


async def test_logout_while_waiting_for_real_pool_blocks_data_access(client,app,settings,config):
    import asyncio
    from psycopg_pool import AsyncConnectionPool
    from psycopg.rows import dict_row
    await login(client,config['users'][0])
    context=await app.state.sessions.require(client.cookies.get(SID_COOKIE))
    database=app.state.database
    original=database.runtime
    database.runtime=AsyncConnectionPool(settings.runtime_dsn,min_size=1,max_size=1,
        open=False,kwargs={'row_factory':dict_row})
    await database.runtime.open(wait=True)
    try:
        async with database.runtime.connection():
            pending=asyncio.create_task(database.list_conversations(context))
            await asyncio.sleep(.05)
            assert database.runtime.get_stats()['requests_waiting']>=1
            await app.state.sessions.revoke(context)
        with pytest.raises(Exception) as error: await pending
        assert error.value.status_code==401
    finally:
        await database.runtime.close()
        database.runtime=original


async def test_composite_owner_fk_and_append_only_audit(client,app,settings,config):
    current,_,_=await login(client,config['users'][0])
    resource=(await client.post('/api/v1/conversations',json={},headers=mutation_headers(settings,current))).json()['id']
    with psycopg.connect(config['dsns']['runtime']) as c:
        with c.transaction():
            c.execute("SELECT set_config('bank.customer_id',%s,true)",(current['customer_id'],))
            c.execute('INSERT INTO banking.conversation_scopes(conversation_id,customer_id) VALUES(%s,%s)',(resource,current['customer_id']))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with c.transaction():
                other=config['users'][1]['customer']
                c.execute("SELECT set_config('bank.customer_id',%s,true)",(other,))
                c.execute('INSERT INTO banking.conversation_scopes(conversation_id,customer_id) VALUES(%s,%s)',(resource,other))
    for statement in ['UPDATE banking.audit_events SET event=event','DELETE FROM banking.audit_events']:
        with psycopg.connect(config['dsns']['runtime']) as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege): c.execute(statement)


async def test_unknown_operations_and_identity_body_rejected(client,settings,config):
    current,_,_=await login(client,config['users'][0])
    for body in [{'customer_id':config['users'][1]['customer']},{'sql':'SELECT *'},{'tool':'arbitrary'}]:
        assert (await client.post('/api/v1/conversations',json=body,headers=mutation_headers(settings,current))).status_code==422


async def test_worker_process_owner_foreign_logout_and_trace_link(client,app,settings,config):
    import asyncio,json,os,subprocess,sys
    from dataclasses import asdict
    from pathlib import Path
    current,_,_=await login(client,config['users'][0])
    resource=(await client.post('/api/v1/conversations',json={},headers=mutation_headers(settings,current))).json()['id']
    ctx=await app.state.sessions.require(client.cookies.get(SID_COOKIE))
    with app.state.telemetry.tracer.start_as_current_span('bank.request') as span:
        parent=span.get_span_context()
    data=dict(context=asdict(ctx),resource_id=resource,trace_id=format(parent.trace_id,'032x'),span_id=format(parent.span_id,'016x'))
    # Worker child receives only its own DB credential, not the API/auth/migrator DSNs.
    env={k:v for k,v in os.environ.items() if not any(part in k for part in ('DATABASE','DSN','SECRET','PASSWORD','API_KEY','TOKEN'))}
    env.update({**config['env'],'BANK_NAMESPACE':settings.namespace,
         'OIDC_CLIENT_SECRET':'worker-does-not-perform-login',
         'BANK_DATABASE_URL':config['dsns']['worker'],'BANK_AUTH_DATABASE_URL':config['dsns']['worker']}
    )
    async def execute(payload):
        proc=await asyncio.to_thread(subprocess.run,[sys.executable,'-m','jobs.banking_probe'],
                  input=json.dumps(payload),capture_output=True,text=True,encoding='utf-8',env=env,
                  cwd=Path(__file__).resolve().parents[2],timeout=15)
        assert proc.returncode==0
        return json.loads(proc.stdout)
    good=await execute(data)
    assert good['status']==200
    assert good['process_id']!=os.getpid()
    assert any(data['trace_id'] in s['links'] for s in good['spans'])
    observation('worker-trace.json',dict(parent_process_id=os.getpid(),worker_process_id=good['process_id'],
        parent_trace_id=data['trace_id'],spans=good['spans'],own_resource_access=True))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=settings.origin) as other:
        await login(other,config['users'][1])
        other_ctx=await app.state.sessions.require(other.cookies.get(SID_COOKIE))
        foreign={**data,'context':asdict(other_ctx)}
        assert (await execute(foreign))['status']==404
    spoof={**data,'context':{**data['context'],'customer_id':config['users'][1]['customer']}}
    assert (await execute(spoof))['status']==401
    await client.post('/api/v1/auth/logout',json={},headers=mutation_headers(settings,current))
    assert (await execute(data))['status']==401
