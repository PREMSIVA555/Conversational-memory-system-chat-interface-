import json,copy
from pathlib import Path
from uuid import uuid4
import pytest,pytest_asyncio
from scripts.p054_probe import JourneyProbe
from store.banking_policy_answers import explain,DECLINE_SUPPORT_TEXT

@pytest_asyncio.fixture
async def p():
    probe=await JourneyProbe().start()
    try:yield probe
    finally:await probe.close()

async def send(p,cid,text,actor='A'):
    accepted=await p.step(dict(op='message',cid=cid,text=text,actor=actor));assert accepted['status']==202
    await p.step({'op':'work'})
    r=await p.step(dict(op='result',cid=cid,id=accepted['body']['turn_id'],actor=actor))
    return r['body']['result']

async def create_case(p):
    cid=(await p.step({'op':'prepare'}))['body']['conversation_id']
    turn=await p.step(dict(op='message',cid=cid,text='Hi'))
    await p.step({'op':'work'})
    preview=await p.step({'op':'draft','body':dict(source_turn_id=turn['body']['turn_id'],topic='general',resource_id=None,summary='Synthetic support follow-up')})
    saved=preview['body'];o=await p.step(dict(op='outcome',id=saved['id']))
    req={k:v for k,v in o['body']['request'].items() if k!='proposal_id'}
    r=await p.step(dict(op='confirm',id=saved['id'],body=req));assert r['status']==200
    return r['body']

@pytest.mark.asyncio
async def test_synthetic_decline_policy_locale_and_unsupported_constraints(p):
    cid=(await p.step({'op':'prepare'}))['body']['conversation_id']
    result=await send(p,cid,'Explain the synthetic credit card decline support procedure for en-IN at 2026-01-15T12:00:00Z')
    assert result['status']=='completed' and DECLINE_SUPPORT_TEXT in result['text']
    assert all(e['citation']['locale']=='en-IN' and e['field']=='decline_support' for e in result['evidence'])
    invalid_date=await send(p,cid,'Explain the credit card decline policy at 2026-01-15')
    assert invalid_date['status']=='clarify' and 'timezone' in invalid_date['text'] and not invalid_date['evidence']
    await p.step(dict(op='preference_accept',text='Always reply in Hindi',save='h'))
    await p.step(dict(op='preference_process',event='$h.outbox_id'))
    hi=await send(p,cid,'सिंथेटिक क्रेडिट कार्ड की अस्वीकृत सहायता प्रक्रिया समझाएँ')
    assert hi['evidence'] and 'सिंथेटिक कार्ड' in hi['text'] and all(e['citation']['locale']=='hi-IN' for e in hi['evidence'])
    for text in ['Explain the credit card decline policy for fr-FR at 2026-01-15T12:00:00Z','Explain the credit card decline policy for en-IN at 2028-01-15T12:00:00Z','Explain the loan decline policy for en-IN at 2026-01-15T12:00:00Z']:
        value=await send(p,cid,text);assert not value['evidence'],value
    assert p.model_calls==0

@pytest.mark.asyncio
async def test_synthetic_procedure_rejects_added_promises_and_wrong_applicability(p):
    cid=(await p.step({'op':'prepare'}))['body']['conversation_id']
    question='Explain the synthetic credit card decline support procedure for en-IN at 2026-01-15T12:00:00Z'
    await p.step(dict(op='policy_change',text=DECLINE_SUPPORT_TEXT+' All card payments will be approved.'))
    rejected=await send(p,cid,question)
    assert not rejected['evidence'] and 'All card payments will be approved' not in rejected['text']
    for change in [dict(product='loan'),dict(scope='business'),dict(locale='fr-FR'),dict(effective_until='2026-01-02T00:00:00Z')]:
        await p.step({'op':'policy_restore'})
        await p.step({'op':'policy_change',**change})
        assert not (await send(p,cid,question))['evidence']
    assert p.model_calls==0

@pytest.mark.asyncio
async def test_latest_hindi_case_reply_ownership_ambiguity_and_freshness(p):
    a=await create_case(p)
    await p.step({'op':'advance','id':a['case_id'],'status':'in_review'})
    await p.step(dict(op='preference_accept',text='Always reply in Hindi',save='h'))
    await p.step(dict(op='preference_process',event='$h.outbox_id'))
    await p.step(dict(op='new_session',actor='A2'))
    cid=(await p.step(dict(op='prepare',actor='A2')))['body']['conversation_id']
    reply=await send(p,cid,'Any update on my complaint?',actor='A2')
    assert 'जाँच में है' in reply['text'] and reply['blocks'][0]['data']['id']==a['case_id']
    await p.step({'op':'advance','id':a['case_id'],'status':'resolved'})
    fresh=await send(p,cid,'मेरी शिकायत की स्थिति क्या है?',actor='A2')
    assert fresh['blocks'][0]['data']['status']=='resolved' and 'समाधान हो गया' in fresh['text']
    b=await create_case(p)
    ambiguous=await send(p,cid,'Any update on my complaint?',actor='A2')
    assert ambiguous['status']=='clarify' and not ambiguous['evidence']
    exact=await send(p,cid,'Status of support request '+a['case_number'],actor='A2')
    assert exact['blocks'][0]['data']['id']==a['case_id']
    bare=await send(p,cid,a['case_number'],actor='A2')
    assert bare['blocks'][0]['data']['id']==a['case_id']
    bc=(await p.step(dict(op='prepare',actor='B')))['body']['conversation_id']
    foreign=await send(p,bc,'Status of support request '+a['case_number'],actor='B')
    assert not foreign['evidence'] and a['case_number'] not in foreign['text']
    assert (await send(p,bc,'Any update on my complaint?',actor='B'))['status']=='no_evidence'
    assert p.model_calls==0

@pytest.mark.asyncio
async def test_case_replies_fail_closed_when_feature_off_or_source_erased(p):
    await create_case(p);p.app.state.turn_worker.provider.support_enabled=False
    cid=(await p.step({'op':'prepare'}))['body']['conversation_id']
    assert not (await send(p,cid,'Any update on my complaint?'))['evidence']
    p.app.state.turn_worker.provider.support_enabled=True
    accepted=await p.step(dict(op='message',cid=cid,text='Any update on my complaint?'))
    await p.step(dict(op='erase',turn_id=accepted['body']['turn_id']))
    await p.step({'op':'work'})
    r=await p.step(dict(op='result',cid=cid,id=accepted['body']['turn_id']))
    assert not (r['body'].get('result') or {}).get('evidence')
