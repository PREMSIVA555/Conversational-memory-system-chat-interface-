"""Atomic local confirmation against guarded Docker PostgreSQL/Redis."""
import asyncio
from uuid import uuid4
import pytest,pytest_asyncio,psycopg
from scripts.p052_probe import ConfirmationProbe,TABLES
from tests.p05.test_proposals import draft,bound

@pytest_asyncio.fixture
async def confirm_probe():
    p=await ConfirmationProbe().start()
    try:yield p
    finally:await p.close()

def request(p,key=None):return {**bound(p),'operation_key':key or str(uuid4())}

@pytest.mark.asyncio
async def test_exact_replay_keys_payload_and_durable_uniqueness(confirm_probe):
    p=confirm_probe;preview=await draft(p);body=request(preview)
    first=await p.step({'op':'confirm','id':preview['id'],'body':body})
    assert first['status']==200,first
    assert first['body']['result']=='case.created'
    assert (await p.step({'op':'confirm','id':preview['id'],'body':body}))==first
    assert (await p.step({'op':'confirm','id':preview['id'],'body':{**body,'payload_hash':'f'*64}}))['status']==409
    other=await p.step({'op':'confirm','id':preview['id'],'body':request(preview)})
    assert other['status']==409 and other['body']['detail']['case_id']==first['body']['case_id']
    await p.step({'op':'expire_receipt','key':body['operation_key']})
    assert (await p.step({'op':'confirm','id':preview['id'],'body':body}))['status']==409
    assert (await p.step({'op':'confirm','id':preview['id'],'body':request(preview)}))['status']==409
    oracle=await p.step({'op':'oracle'})
    assert set(oracle['counts'].values())=={1} and oracle['proposals'][0][3]=='consumed'
    assert oracle['bank_data_unchanged'] and not oracle['direct_runtime_writes'] and oracle['model_calls']==0

@pytest.mark.asyncio
@pytest.mark.parametrize('stage',[*TABLES,'support_proposals','commit'])
async def test_each_storage_fault_rolls_back_then_same_key_retries(confirm_probe,stage):
    p=confirm_probe;preview=await draft(p);body=request(preview)
    await p.step({'op':'fault','stage':stage})
    result=await p.step({'op':'confirm','id':preview['id'],'body':body})
    assert result['status']>=500,result
    oracle=await p.step({'op':'oracle'})
    assert set(oracle['counts'].values())=={0} and oracle['proposals'][0][3]=='pending'
    await p.step({'op':'clear_fault'})
    assert (await p.step({'op':'confirm','id':preview['id'],'body':body}))['status']==200
    assert set((await p.step({'op':'oracle'}))['counts'].values())=={1}

@pytest.mark.asyncio
async def test_parallel_same_and_different_keys_and_edit_confirm(confirm_probe):
    p=confirm_probe;preview=await draft(p);body=request(preview)
    values=await asyncio.gather(*(p.step({'op':'confirm','id':preview['id'],'body':body}) for _ in range(6)))
    assert all(x==values[0] and x['status']==200 for x in values)
    next_preview=await draft(p)
    values=await asyncio.gather(*(p.step({'op':'confirm','id':next_preview['id'],'body':request(next_preview)}) for _ in range(6)))
    assert sorted(x['status'] for x in values)==[200,409,409,409,409,409]
    next_preview=await draft(p)
    confirm,edit=await asyncio.gather(p.step({'op':'confirm','id':next_preview['id'],'body':request(next_preview)}),p.step({'op':'edit','id':next_preview['id'],'body':{**bound(next_preview),'draft':{**next_preview['payload'],'summary':'Changed'}}}))
    assert sorted([confirm['status'],edit['status']]) in ([200,409],[201,409])
    o=await p.step({'op':'oracle'});assert len(set(o['counts'].values()))==1

@pytest.mark.asyncio
async def test_ownership_session_expiry_cancel_and_body_guards(confirm_probe):
    p=confirm_probe;preview=await draft(p);body=request(preview)
    for actor in ['B','A2']:
        assert (await p.step({'op':'confirm','actor':actor,'id':preview['id'],'body':body}))['status']==404
    assert (await p.step({'op':'confirm','id':preview['id'],'body':{**body,'customer_id':'forged'}}))['status']==422
    assert (await p.step({'op':'confirm','id':preview['id'],'body':body,'headers':{'X-CSRF-Token':''}}))['status']==403
    await p.step({'op':'expire','id':preview['id']})
    assert (await p.step({'op':'confirm','id':preview['id'],'body':body}))['status']==409
    preview=await draft(p);await p.step({'op':'cancel','id':preview['id'],'body':bound(preview)})
    assert (await p.step({'op':'confirm','id':preview['id'],'body':request(preview)}))['status']==409
    assert set((await p.step({'op':'oracle'}))['counts'].values())=={0}

@pytest.mark.asyncio
async def test_erasure_scrubs_case_text_blocks_replay_and_keeps_uniqueness(confirm_probe):
    p=confirm_probe;preview=await draft(p);body=request(preview)
    assert (await p.step({'op':'confirm','id':preview['id'],'body':body}))['status']==200
    assert (await p.step({'op':'erase','turn_id':preview['source_turn_id']}))['status']==200
    assert (await p.step({'op':'confirm','id':preview['id'],'body':body}))['status']==404
    o=await p.step({'op':'oracle'});assert not o['proposals'] and o['cases'][0][-1] is True and set(o['counts'].values())=={1}

@pytest.mark.asyncio
async def test_optional_exporter_failure_preserves_committed_receipt(confirm_probe):
    p=confirm_probe;preview=await draft(p);body=request(preview)
    await p.step({'op':'telemetry_fault'})
    first=await p.step({'op':'confirm','id':preview['id'],'body':body});assert first['status']==200
    assert (await p.step({'op':'telemetry_flush'}))['optional_exporter_failures']>0
    assert await p.step({'op':'confirm','id':preview['id'],'body':body})==first
    assert set((await p.step({'op':'oracle'}))['counts'].values())=={1}

@pytest.mark.asyncio
@pytest.mark.parametrize('language,text,forbidden',[('en','Help','case submission are not available'),('hi','मदद','केस जमा करना उपलब्ध नहीं है')])
async def test_help_guides_to_review_without_false_submission_denial(confirm_probe,language,text,forbidden):
    p=confirm_probe
    response=await p.step({'op':'workspace_turn','text':text,'fields':{'instructions':{'language':language,'format':'concise'}}})
    assert response['status']==200 and response['body']['result']['message_kind']=='help'
    answer=response['body']['result']['text']
    assert forbidden not in answer
    assert ('support' if language=='en' else 'सहायता') in answer
    assert set((await p.step({'op':'oracle'}))['counts'].values())=={0}
