import asyncio
from uuid import uuid4
import pytest,pytest_asyncio
import psycopg
from scripts.p053_probe import ContinuityProbe
from tests.p05.test_proposals import draft,bound

@pytest_asyncio.fixture
async def continuity():
    p=await ContinuityProbe().start()
    try:yield p
    finally:await p.close()

async def original(p,preview):
    r=await p.step({'op':'outcome','id':preview['id']});assert r['status']==200
    full=r['body']['request'];return {k:v for k,v in full.items() if k!='proposal_id'}

@pytest.mark.asyncio
async def test_existing_committed_receipt_is_backfilled_without_rewriting_legacy_effects():
    from scripts.p052_probe import ConfirmationProbe
    from evals.banking_world import database_dsn
    from store.banking_migrate import apply
    from store.banking_continuity import Continuity
    p=await ConfirmationProbe().start()
    try:
        preview=await draft(p);req={**bound(preview),'operation_key':str(uuid4())}
        response=await p.step({'op':'confirm','id':preview['id'],'body':req});assert response['status']==200
        before=await p.step({'op':'oracle'})
        p.world.guard()
        with psycopg.connect(database_dsn(p.world.config['dsns']['migrator'],p.world.name)) as c:
            apply(c,p.world.config['roles'],through='0032')
        after=await p.step({'op':'oracle'})
        for key in ('operations','cases','counts','audit','outbox'):
            assert before[key]==after[key],key
        service=Continuity(p.app.state.database)
        outcome=await service.outcome(p.contexts['A'],preview['id'])
        assert outcome['accepted'] and outcome['outcome']=='committed'
        assert outcome['request']['operation_key']==req['operation_key']
        assert outcome['creation_receipt']==response['body']
        assert outcome['current_case']['status']=='created'
        assert outcome['current_case']['status_updated_at']==outcome['current_case']['created_at']
        assert (await p.step({'op':'confirm','id':preview['id'],'body':req}))==response
    finally:await p.close()

@pytest.mark.asyncio
async def test_read_only_reconciliation_stable_identity_and_fresh_later_session_status(continuity):
    p=continuity;preview=await draft(p);request=await original(p,preview)
    assert await original(p,preview)==request
    for _ in range(2):
        r=await p.step({'op':'reconcile','id':preview['id'],'body':request})
        assert r['body']['outcome']=='not_recorded' and not r['body']['accepted']
    o=await p.step({'op':'oracle'});assert not o['intents'] and set(o['counts'].values())=={0}
    response=await p.step({'op':'confirm','id':preview['id'],'body':request});assert response['status']==200,response
    receipt=response['body'];await p.step({'op':'advance','id':receipt['case_id'],'status':'in_review'})
    now=await p.step({'op':'reconcile','id':preview['id'],'body':request})
    assert now['body']['creation_receipt']==receipt and now['body']['current_case']['status']=='in_review'
    assert now['body']['current_case']['checked_at']>=now['body']['current_case']['status_updated_at']
    later=await p.step({'op':'case','actor':'A2','id':receipt['case_id']});assert later['body']['status']=='in_review'
    assert (await p.step({'op':'outcome','actor':'A2','id':preview['id']}))['status']==404
    assert (await p.step({'op':'case','actor':'B','id':receipt['case_id']}))['status']==404
    assert 'operation_key' not in later['body'] and 'payload' not in later['body']
    assert (await p.step({'op':'case_write','id':receipt['case_id'],'body':{'status':'resolved'}}))['status']==405
    o=await p.step({'op':'oracle'});assert not o['runtime_can_advance'] and o['bank_data_unchanged'] and o['model_calls']==0

@pytest.mark.asyncio
async def test_intent_survives_effect_failure_and_lookup_never_retries_creation(continuity):
    p=continuity;preview=await draft(p);request=await original(p,preview)
    await p.step({'op':'fault','stage':'support_cases'})
    assert (await p.step({'op':'confirm','id':preview['id'],'body':request}))['status']==503
    for _ in range(2):
        result=await p.step({'op':'outcome','id':preview['id']});assert result['body']['outcome']=='not_recorded' and result['body']['accepted']
        assert {k:v for k,v in result['body']['request'].items() if k!='proposal_id'}==request
    o=await p.step({'op':'oracle'});assert len(o['intents'])==1 and set(o['counts'].values())=={0}
    wrong={**request,'operation_key':str(uuid4())}
    assert (await p.step({'op':'confirm','id':preview['id'],'body':wrong}))['status']==409
    assert (await p.step({'op':'reconcile','id':preview['id'],'body':wrong}))['status']==409
    await p.step({'op':'clear_fault'})
    assert (await p.step({'op':'confirm','id':preview['id'],'body':request}))['status']==200
    assert set((await p.step({'op':'oracle'}))['counts'].values())=={1}

@pytest.mark.asyncio
async def test_retention_erasure_and_owned_pagination(continuity):
    p=continuity;created=[]
    for _ in range(3):
        preview=await draft(p);req=await original(p,preview)
        r=await p.step({'op':'confirm','id':preview['id'],'body':req});assert r['status']==200
        created.append((preview,req,r['body']))
    page=await p.step({'op':'cases','params':{'limit':2}});assert len(page['body']['items'])==2
    next_page=await p.step({'op':'cases','params':{'cursor':page['body']['next_cursor'],'limit':2}});assert len(next_page['body']['items'])==1
    assert not (await p.step({'op':'cases','actor':'B'}))['body']['items']
    assert (await p.step({'op':'cases','actor':'B','params':{'cursor':page['body']['next_cursor']}}))['status']==404
    preview,req,receipt=created[0]
    await p.step({'op':'expire_receipt','key':req['operation_key']})
    result=await p.step({'op':'outcome','id':preview['id']});assert result['body']['outcome']=='committed' and result['body']['creation_receipt'] is None
    assert result['body']['current_case']['id']==receipt['case_id']
    assert (await p.step({'op':'erase','turn_id':preview['source_turn_id']}))['status']==200
    assert (await p.step({'op':'outcome','id':preview['id']}))['status']==404
    assert (await p.step({'op':'case','actor':'A2','id':receipt['case_id']}))['status']==200
    assert set((await p.step({'op':'oracle'}))['counts'].values())=={3}

@pytest.mark.asyncio
async def test_cancel_expiry_auth_and_parallel_reconciliation(continuity):
    p=continuity;preview=await draft(p);req=await original(p,preview)
    assert (await p.step({'op':'reconcile','id':preview['id'],'body':req,'headers':{'X-CSRF-Token':''}}))['status']==403
    await p.step({'op':'cancel','id':preview['id'],'body':bound(preview)})
    assert (await p.step({'op':'outcome','id':preview['id']}))['body']['outcome']=='not_submitted'
    preview=await draft(p);req=await original(p,preview);await p.step({'op':'expire','id':preview['id']})
    assert (await p.step({'op':'outcome','id':preview['id']}))['body']['request'] is None
    preview=await draft(p);req=await original(p,preview)
    values=await asyncio.gather(p.step({'op':'confirm','id':preview['id'],'body':req}),p.step({'op':'reconcile','id':preview['id'],'body':req}))
    assert values[0]['status']==200 and values[1]['status']==200
    assert values[1]['body']['outcome'] in ('not_recorded','committed')
    assert (await p.step({'op':'reconcile','id':preview['id'],'body':req}))['body']['outcome']=='committed'
    await p.step({'op':'revoke'})
    assert (await p.step({'op':'cases'}))['status']==401
