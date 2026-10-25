"""Thirty observed checkpoints through production services; no paid model calls.

This is a bounded scenario, not the reusable P06 evaluator. A response hook
withholds a real committed HTTP response, or a guarded DB fault rolls it back.
"""
import asyncio,json,sys,hashlib
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import httpx
from scripts.p054_probe import JourneyProbe

async def run(mode='lost_ack'):
    if mode not in ('lost_ack','audit_rollback'):raise ValueError('Unsupported fault')
    p=await JourneyProbe().start();trace=[]
    source=json.loads((ROOT/'.genesis/sources/T05.4_SOURCE.json').read_text(encoding='utf-8'))
    def observed(number,**facts):
        assert number==len(trace)+1
        trace.append({'step':number,'source_title':source['steps'][number-1]['title'],'observed':facts})
    async def step(op,**kw):return await p.step({'op':op,**kw})
    async def result(cid,tid):
        await step('work');r=await step('result',cid=cid,id=tid)
        assert r['status']==200 and r['body']['result'] is not None,r
        return r['body']['result']
    try:
        # Server-issued synthetic authentication, not an OIDC password simulation.
        prepared=await step('prepare');assert prepared['status']==200
        cid=prepared['body']['conversation_id'];observed(1,authenticated=True,session_kind='server-issued synthetic')
        cards=await step('workspace_products',kind='card');assert cards['status']==200
        items=cards['body']['items'];assert len(items)==3
        observed(2,owned_product_ids=[x['id'] for x in items],customer_not_client_selected=True)
        accepted=await step('message',cid=cid,text='Why did my INR2000 card payment yesterday fail?')
        assert accepted['status']==202;tid=accepted['body']['turn_id'];observed(3,accepted_turn_id=tid)
        pending=await step('result',cid=cid,id=tid);assert pending['body']['result'] is None
        observed(4,durable_pending_turn=True)
        before=await step('preferences');assert before['body']['effective']['preferred_response_language']['value']=='en'
        observed(5,initial_language='en',absolute_version_claim=False)
        choices=await result(cid,tid);assert len(choices['actions'])==3
        assert all(a['resource_selection']['kind']=='card' for a in choices['actions'])
        observed(6,choice_count=3,no_product_guessed=True)
        card=next(x for x in items if x['last_four']=='4112')
        selection={'kind':'card','id':card['id'],'version':card['source']['version']}
        observed(7,selected_owned_id=card['id'],suffix='4112',suffix_is_not_authority=True)
        accepted=await step('message',cid=cid,text='Transactions from 2026-01-14 to 2026-01-15',fields={'action':'transactions','resource_selection':selection})
        assert accepted['status']==202;source_turn=accepted['body']['turn_id'];observed(8,accepted_turn_id=source_turn,validated_selection=selection)
        answer=await result(cid,source_turn);assert answer['status']=='completed',answer
        rows=answer['blocks'][0]['data']['items'];declined=next(x for x in rows if x['status']=='declined')
        assert declined['amount']['minor_units']=='200000' and declined['reason']=='merchant restriction'
        observed(9,transaction=declined,evidence_count=len(answer['evidence']))
        accepted_policy=await step('message',cid=cid,text='Explain the synthetic credit card decline support procedure for en-IN at 2026-01-15T12:00:00Z')
        policy=await result(cid,accepted_policy['body']['turn_id'])
        assert policy['status']=='completed' and policy['evidence'],policy
        assert 'CARD-SUPPORT-v1' in policy['text'] and 'does not determine a decline cause' in policy['text']
        observed(10,policy_text=policy['text'],policy_evidence=policy['evidence'],synthetic=True)
        assert 'merchant restriction' in json.dumps(answer)
        observed(11,checked_answer=answer,reason_from_bank_record=True,model_calls=p.model_calls)
        old=await step('preference_accept',text='Always reply in English')
        new=await step('preference_accept',text='Always reply in Hindi')
        assert old['body']['source_sequence']<new['body']['source_sequence']
        observed(12,older_event=old['body']['event_id'],newer_event=new['body']['event_id'],grammar='Always reply in Hindi')
        assert new['body']['status']=='pending'
        observed(13,acceptance=new['body'],saved_claim=False)
        await step('preference_process',event=new['body']['outbox_id'])
        observed(14,newer_hindi_dispatched=True)
        await step('preference_process',event=old['body']['outbox_id'])
        state=await step('oracle');events=dict(state['preference_events'])
        assert events[str(old['body']['event_id'])]=='ignored_stale' and events[str(new['body']['event_id'])]=='persisted'
        observed(15,older_completion=events[str(old['body']['event_id'])],newer_completion=events[str(new['body']['event_id'])])
        preferences=await step('preferences');assert preferences['body']['effective']['preferred_response_language']['value']=='hi'
        accepted=await step('message',cid=cid,text='Hi');greeting=await result(cid,accepted['body']['turn_id'])
        assert greeting['message_kind']=='greeting' and any('\u0900'<=x<='\u097f' for x in greeting['text'])
        observed(16,persisted_language='hi',actual_greeting=greeting['text'])
        draft=await step('draft',body={'source_turn_id':source_turn,'topic':'card','resource_id':card['id'],'summary':'Synthetic request: review the recorded INR2000 declined payment.'})
        assert draft['status']==201;preview=draft['body'];observed(17,proposal_id=preview['id'],owned_source=source_turn)
        assert preview['payload']['resource_id']==card['id'];observed(18,exact_preview=preview,description_origin='user supplied; no model confirmation')
        original=await step('outcome',id=preview['id']);request={k:v for k,v in original['body']['request'].items() if k!='proposal_id'}
        observed(19,explicit_confirmation=request)
        if mode=='audit_rollback':await step('fault',stage='support_action_audit')
        async def drop(response):
            if response.request.url.path.endswith('/confirm'):raise httpx.ReadError('Intentional post-commit acknowledgement loss',request=response.request)
        if mode=='lost_ack':p.clients['A'].event_hooks['response'].append(drop)
        try:
            first=await step('confirm',id=preview['id'],body=request)
            assert mode=='audit_rollback' and first['status']==503,first
            observed(20,client_status=503,injected='mandatory audit insert failure')
        except httpx.ReadError:
            assert mode=='lost_ack';observed(20,client_status='transport unknown',injected='real HTTP response withheld after commit')
        finally:p.clients['A'].event_hooks['response'].clear()
        state=await step('oracle');expected=1 if mode=='lost_ack' else 0
        assert set(state['counts'].values())=={expected}
        observed(21,atomic_effect_counts=state['counts'],intent_count=len(state['intents']))
        checked=await step('reconcile',id=preview['id'],body=request)
        assert checked['status']==200 and checked['body']['accepted']
        assert checked['body']['outcome']==('committed' if expected else 'not_recorded')
        observed(22,read_only_outcome=checked['body'],no_new_key=True)
        if mode=='audit_rollback':await step('clear_fault')
        retry=await step('confirm',id=preview['id'],body=request);assert retry['status']==200
        receipt=retry['body'];observed(23,explicit_original_key_retry=receipt,operation_key=request['operation_key'])
        same=await step('confirm',id=preview['id'],body=request);assert same==retry
        conflict=await step('confirm',id=preview['id'],body={**request,'operation_key':str(uuid4())});assert conflict['status']==409
        state=await step('oracle');assert set(state['counts'].values())=={1}
        observed(24,one_case_and_audit=True,receipt=receipt,changed_key_status=409)
        assert state['outbox'][0][0]==receipt['case_id'] and state['outbox'][0][-1]=='pending'
        observed(25,durable_reference=receipt['case_id'],outbox=state['outbox'],adaptation='Owned canonical case history plus queued outbox; no semantic-memory consumer/delivery claimed')
        day=await step('next_day');await step('advance',id=receipt['case_id'],status='in_review')
        await step('revoke',actor='A');await step('new_session',actor='A2')
        observed(26,clock_event=day,fixture_status_event='in_review',fresh_session=True)
        prefs=await step('preferences',actor='A2');cases=await step('cases',actor='A2')
        assert prefs['body']['effective']['preferred_response_language']['value']=='hi' and len(cases['body']['items'])==1
        assert (await step('outcome',actor='A2',id=preview['id']))['status']==404
        observed(27,persisted_language='hi',owned_case_reference=cases['body']['items'][0]['id'],old_session_preview_denied=True)
        current=await step('case',actor='A2',id=receipt['case_id']);assert current['status']==200
        observed(28,fresh_case_read=current['body'])
        assert current['body']['status']=='in_review' and current['body']['checked_at']>=current['body']['status_updated_at']
        observed(29,current_status='in_review',creation_receipt_unchanged=receipt)
        assert (await step('case',actor='B',id=receipt['case_id']))['status']==404
        assert not (await step('cases',actor='B'))['body']['items']
        state=await step('oracle');assert state['bank_data_unchanged'] and state['model_calls']==0 and not state['runtime_can_advance']
        assert set(state['counts'].values())=={1} and len(trace)==29
        fresh=await step('prepare',actor='A2');fresh_cid=fresh['body']['conversation_id']
        follow=await step('message',actor='A2',cid=fresh_cid,text='Any update on my complaint?')
        await step('work');reply=await step('result',actor='A2',cid=fresh_cid,id=follow['body']['turn_id'])
        checked_reply=reply['body']['result']
        assert checked_reply['status']=='completed' and 'जाँच में है' in checked_reply['text'],checked_reply
        assert checked_reply['blocks'][0]['data']['status']=='in_review'
        assert checked_reply['evidence'][0]['authority']=='fresh_authorized_support'
        observed(30,final_oracle=state,unauthorized_lookup_denied=True,language='hi',current_status='in_review',actual_checked_reply=checked_reply)
        return {'task':'T05.4','fault':mode,'status':'passed','checkpoints':trace,'initial_semantic_hash':p.before,'source_sha256':source['sha256'],'model_kind':'no model dispatch; checked local paths','paid_model_calls':0,'limitations':['Synthetic server-issued sessions; not an OIDC certification','Next day is a declared domain event; no elapsed24h claim; security clocks unchanged','Synthetic procedure cannot determine decline cause or promise approval/resolution/delivery','Step25 verifies canonical history/queued outbox, not a semantic-memory consumer','Natural complaint follow-up is bounded to a sole owned case or explicit owned SR reference; multiple cases clarify']}
    except Exception as error:
        path=ROOT/f'.genesis/evidence/T05.4-{mode}-failed-{uuid4()}.json'
        path.write_text(json.dumps({'task':'T05.4','status':'failed','fault':mode,'completed_checkpoints':trace,'error':str(error),'error_type':type(error).__name__},indent=2,default=str)+'\n',encoding='utf-8')
        raise
    finally:await p.close()

async def main():
    mode=sys.argv[1] if len(sys.argv)>1 else 'lost_ack'
    report=await run(mode);path=ROOT/f'.genesis/evidence/T05.4-{mode}.json'
    path.write_text(json.dumps(report,indent=2,default=str)+'\n',encoding='utf-8')
    print(json.dumps({'status':report['status'],'checkpoints':len(report['checkpoints']),'report':str(path)}))
if __name__=='__main__':asyncio.run(main())
