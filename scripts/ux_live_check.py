"""User-authorized capped live evaluation. Reservations count failed requests too."""
import asyncio,json,os,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP','True')
from store.db import load_env
load_env()
from llm import config as llm
from api.privacy import model_input
from scripts.ux_probe import Probe
from tests.ux.test_workspace import send,prepare,selection

BUDGET=ROOT/'.genesis/evidence/UX01-live-budget.json'
REPORT=ROOT/'.genesis/evidence/UX01-live-report.json'

class CountedTransport:
    async def acompletion(self,**kwargs):
        # One process runs this evaluation sequentially. Reserve on disk before
        # dispatch so process death cannot reset the authorized total.
        state=json.loads(BUDGET.read_text(encoding='utf-8')) if BUDGET.exists() else dict(limit=20,attempts=[])
        if len(state['attempts'])>=20:raise RuntimeError('Live request cap reached')
        model=llm.resolve_completion_model()
        if 'astra' in model.casefold():raise RuntimeError('Astra requires explicit confirmation')
        entry=dict(number=len(state['attempts'])+1,configured_model=model,status='reserved')
        state['attempts'].append(entry);BUDGET.write_text(json.dumps(state,indent=2),encoding='utf-8')
        started=time.monotonic()
        try:
            value=await llm._litellm().acompletion(**kwargs)
            entry.update(status='returned',model=getattr(value,'model',None))
            return value
        except Exception as error:
            message=str(error)
            for key,value in os.environ.items():
                if any(part in key.upper() for part in ('KEY','TOKEN','SECRET','PASSWORD','DSN')) and len(value)>3:
                    message=message.replace(value,'[redacted]')
            entry.update(status='failed',error=type(error).__name__,detail=message[:1500]);raise
        finally:
            entry['elapsed_ms']=round((time.monotonic()-started)*1000,2)
            BUDGET.write_text(json.dumps(state,indent=2),encoding='utf-8')

async def main():
    await llm.bounded_completion_capabilities(timeout=60)
    await asyncio.to_thread(model_input,'Synthetic banking question')
    p=await Probe().start();p.app.state.turn_worker.transport=CountedTransport()
    results=json.loads(REPORT.read_text(encoding='utf-8')).get('cases',[]) if REPORT.exists() else []
    try:
        c=p.clients[0];cid=await prepare(c)
        credit=next(x for x in (await c.get('/api/v1/products/card')).json()['items'] if x['kind']=='credit')
        loan=(await c.get('/api/v1/products/loan')).json()['items'][0]
        cases=[
          ('greeting','Hi',{}),
          ('card-explanation','Explain limits for this card',{'resource_selection':selection(credit,'card'),'action':'card_details'}),
          ('hindi-card','इस कार्ड की जानकारी समझाइए',{'resource_selection':selection(credit,'card'),'action':'card_details','instructions':{'language':'hi'}}),
          ('schedule','Explain the EMI schedule from 2026-01-01 to 2026-01-31',{'resource_selection':selection(loan,'loan'),'action':'schedule'}),
          ('payments','Explain the payment records from 2026-01-01 to 2026-01-31',{'resource_selection':selection(loan,'loan'),'action':'payments'}),
          ('policy-fee','Explain credit card annual fee locale en-IN scope retail at 2026-06-01T00:00:00Z',{}),
          ('policy-waiver','Explain credit card fee waiver locale en-IN scope retail at 2026-06-01T00:00:00Z',{}),
          ('wrong-locale','Explain credit card annual fee locale de-DE scope retail at 2026-06-01T00:00:00Z',{}),
          ('conflicting-locales','Explain credit card annual fee locale en-IN and locale de-DE at 2026-06-01T00:00:00Z',{}),
          ('decline-cause','Why was my card transaction declined from 2026-01-14 to 2026-01-15?',{'resource_selection':selection(credit,'card'),'action':'transactions'}),
        ]
        if '--final-two' in sys.argv:cases=cases[1:3]
        for name,text,fields in cases:
            if BUDGET.exists() and len(json.loads(BUDGET.read_text())['attempts'])>=20:break
            started=time.monotonic()
            try:
                result=await send(c,cid,text,**fields)
                results.append(dict(name=name,text=text,result=result,elapsed_ms=round((time.monotonic()-started)*1000,2)))
            except Exception as error:results.append(dict(name=name,status='harness_failed',error=type(error).__name__,elapsed_ms=round((time.monotonic()-started)*1000,2)))
            REPORT.write_text(json.dumps(dict(cases=results,budget=json.loads(BUDGET.read_text()) if BUDGET.exists() else {'attempts':[]},scope='Actual configured non-Astra model; scripted and human ratings are separate. No universal wording quality or SLA claim.'),indent=2,ensure_ascii=False),encoding='utf-8')
        print(json.dumps({'cases':len(results),'requests':len(json.loads(BUDGET.read_text())['attempts']) if BUDGET.exists() else 0,'report':str(REPORT.relative_to(ROOT))}))
    finally:await p.close()
if __name__=='__main__':asyncio.run(main())
