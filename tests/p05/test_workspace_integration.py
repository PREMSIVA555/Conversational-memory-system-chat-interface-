"""T05.1 on actual workspace turns and owned-product APIs, not a UI-only mock."""
import asyncio,hashlib,json
import pytest
import psycopg
from psycopg.types.json import Jsonb
from tests.p05.test_proposals import probe, bound


async def source(p, kind='account'):
    page = await p.step({'op':'workspace_products','kind':kind})
    assert page['status']==200
    product=page['body']['items'][0]
    action={'account':'balance','card':'card_details','loan':'loan_details'}[kind]
    turn=await p.step({'op':'workspace_turn','text':'View this product','fields':{
        'action':action,'instructions':{'format':'concise','language':'en'},
        'resource_selection':{'kind':kind,'id':product['id'],'version':product['source']['version']}}})
    assert turn['status']==200,turn
    assert turn['body']['result']['workspace']['resource']['id']==product['id']
    return product,turn['body']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['account','card','loan'])
async def test_workspace_source_owned_label_and_exact_immutable_preview(probe,kind):
    product,turn=await source(probe,kind)
    draft={'source_turn_id':turn['turn_id'],'topic':kind,'resource_id':product['id'],'summary':'  Synthetic issue\r\nPlease review <b>as text</b>.  '}
    r=await probe.step({'op':'draft','body':draft})
    assert r['status']==201,r
    saved=r['body'];label=product['display_name']
    if kind=='card':label+=f" · {product['kind']} •••• {product['last_four']}"
    assert saved['resource_label']==label
    assert saved['payload']['summary']=='Synthetic issue\nPlease review <b>as text</b>.'
    expected=hashlib.sha256(json.dumps(saved['payload'],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    assert saved['payload_hash']==expected and 'resource_label' not in saved['payload']
    assert (await probe.step({'op':'latest','turn_id':turn['turn_id']}))['body']==saved
    changed=await probe.step({'op':'edit','id':saved['id'],'body':{**bound(saved),'draft':{**saved['payload'],'summary':'Revised synthetic issue'}}})
    assert changed['status']==201 and changed['body']['version']==2
    assert changed['body']['resource_label']==label and changed['body']['id']!=saved['id']
    assert (await probe.step({'op':'get','id':saved['id']}))['body']['status']=='cancelled'
    assert (await probe.step({'op':'confirm','id':changed['body']['id'],'body':bound(changed['body'])}))['status']==503
    oracle=await probe.step({'op':'oracle'})
    assert oracle['bank_data_unchanged'] and oracle['case_table'] is None and oracle['model_calls']==0


@pytest.mark.asyncio
async def test_workspace_preview_cannot_cross_customer_or_session(probe):
    product,turn=await source(probe)
    payload={'source_turn_id':turn['turn_id'],'topic':'account','resource_id':product['id'],'summary':'Owned preview'}
    for actor in ['A2','B']:
        assert (await probe.step({'op':'draft','actor':actor,'body':payload}))['status']==404
    foreign={**payload,'resource_id':probe.saved['account_b']}
    assert (await probe.step({'op':'draft','body':foreign}))['status']==404
    good=await probe.step({'op':'draft','body':payload});assert good['status']==201
    for actor in ['A2','B']:
        for op in ['get','latest','cancel']:
            r=await probe.step({'op':op,'actor':actor,'id':good['body']['id'],'turn_id':turn['turn_id'],'body':bound(good['body'])})
            assert r['status']==404


@pytest.mark.asyncio
async def test_workspace_source_erasure_removes_context_and_preview(probe):
    product,turn=await source(probe)
    saved=await probe.step({'op':'draft','body':{'source_turn_id':turn['turn_id'],'topic':'account','resource_id':product['id'],'summary':'Erase this synthetic preview'}})
    assert saved['status']==201
    assert await probe.app.state.workspace.context(probe.contexts['A'],turn['conversation_id'])
    assert (await probe.step({'op':'erase','turn_id':turn['turn_id']}))['status']==200
    assert (await probe.step({'op':'get','id':saved['body']['id']}))['status']==404
    assert await probe.app.state.workspace.context(probe.contexts['A'],turn['conversation_id']) is None
    assert not (await probe.step({'op':'oracle'}))['proposals']


@pytest.mark.asyncio
@pytest.mark.parametrize('editing',[False,True])
async def test_erasure_races_cannot_leave_new_proposal_rows(probe,editing):
    for attempt in range(6):
        product,turn=await source(probe)
        draft={'source_turn_id':turn['turn_id'],'topic':'account','resource_id':product['id'],'summary':f'Erasure race {attempt}'}
        if editing:
            saved=await probe.step({'op':'draft','body':draft});assert saved['status']==201
            mutation={'op':'edit','id':saved['body']['id'],'body':{**bound(saved['body']),'draft':{**draft,'summary':'New version must also be erased'}}}
        else:mutation={'op':'draft','body':draft}
        write,erased=await asyncio.gather(probe.step(mutation),probe.step({'op':'erase','turn_id':turn['turn_id']}))
        assert write['status'] in (201,404),write
        assert erased['status']==200,erased
        assert (await probe.step({'op':'latest','turn_id':turn['turn_id']}))['status']==404
        oracle=await probe.step({'op':'oracle'})
        assert oracle['proposals']==[] and oracle['model_calls']==0 and oracle['bank_data_unchanged']


@pytest.mark.asyncio
async def test_forward_migration_removes_preexisting_erased_preview_residue():
    from evals.workspace_world import WorkspaceWorld
    from evals.banking_world import database_dsn
    from scripts.p051_probe import Probe
    from store.banking_preferences import binding
    from store.banking_migrate import apply
    p=await Probe().start(world_class=WorkspaceWorld) # real pre0030 private schema
    try:
        turn=await p.step({'op':'turn'});assert turn['status']==202
        assert (await p.step({'op':'erase','turn_id':turn['body']['turn_id']}))['status']==200
        payload={'source_turn_id':turn['body']['turn_id'],'topic':'general','resource_id':None,'summary':'Simulated residue from pre-fence erasure/edit race'}
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        p.world.guard() # test-only old-race residue, never an ordinary-demo write
        with psycopg.connect(p.world.admin_dsn) as c:
            c.execute('''INSERT INTO banking.support_proposals(customer_id,source_turn_id,session_binding,version,payload,payload_hash)
                      VALUES(%s,%s,%s,2,%s,%s)''',(p.contexts['A'].customer_id,turn['body']['turn_id'],binding(p.contexts['A']),Jsonb(payload),digest))
            assert c.execute('SELECT count(*) FROM banking.support_proposals').fetchone()[0]==1
        with psycopg.connect(database_dsn(p.world.config['dsns']['migrator'],p.world.name)) as c:
            apply(c,p.world.config['roles'],through='0030')
        assert (await p.step({'op':'oracle'}))['proposals']==[]
        assert (await p.step({'op':'draft','body':payload}))['status']==404
    finally:await p.close()
