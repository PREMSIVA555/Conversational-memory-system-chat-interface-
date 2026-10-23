"""JSON black-box H01 probes; scripted SDK only, actual policy/API/worker/SQL."""
from pathlib import Path
import asyncio,json,os,sys
from copy import deepcopy
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from api.privacy import model_input
from scripts import p044_probe as existing
from scripts.p03_slice import RawScript
from llm.config import bounded_completion_capabilities

PHASE='setup'


class CapturedScript(RawScript):
    def __init__(self):super().__init__();self.messages=[]
    async def acompletion(self,**kwargs):
        self.messages.append(deepcopy(kwargs['messages']))
        return await super().acompletion(**kwargs)


def unavailable_scanner():raise RuntimeError('Synthetic local scanner outage')


async def turn(req):
    global PHASE
    PHASE='pre-acceptance SDK metadata initialization'
    await bounded_completion_capabilities(timeout=60)
    PHASE='API flow'
    script=CapturedScript()
    with patch.object(existing,'RawScript',lambda:script):
        value=await existing.api_turn(existing.world_for(req['world']),req)
    return value|dict(model_messages=script.messages,metadata_warmed_before_acceptance=True)


def operate(req):
    if req['op']=='batch':return [operate(case) for case in req['cases']]
    if req['op']=='create':
        from scripts.p043_probe import operate as catalog
        value=catalog(req)
        if req.get('seed',False):
            manifest=json.loads((ROOT/'evals/fixtures/p04_policy_queries.json').read_text(encoding='utf-8'))['manifest']
            staged=catalog(dict(op='stage',world=value['world'],manifest=manifest))
            catalog(dict(op='activate',world=value['world'],set_id=staged['set_id']))
            value['catalog']=manifest
        return value
    if req['op'] not in ('sanitize','turn'):return existing.operate(req)
    if type(req.get('text')) is not str or len(req['text'])>8000:raise ValueError('Bounded synthetic text required')
    if 'secret' in req:
        if not req['secret'].startswith('SYNTHETIC_'):raise ValueError('Synthetic secret required')
    env={'SYNTHETIC_API_KEY':req.get('secret',''),'LLM_STRUCTURED_JSON_ENABLED':'true'}
    if req.get('missing_model',False):env['PII_SPACY_MODEL']='h01_synthetic_model_not_installed'
    with patch.dict(os.environ,env):
        if req.get('scanner_failure',False):
            with patch('capture.pii.get_engines',unavailable_scanner):
                if req['op']=='turn':return asyncio.run(turn(req))
                try:model_input(req['text'])
                except ValueError:return dict(blocked=True,text=None)
                raise AssertionError('Privacy scan unexpectedly passed')
        if req['op']=='sanitize':
            try:return dict(text=model_input(req['text']),scope='function only; no model dispatched')
            except ValueError:
                if req.get('missing_model',False):return dict(blocked=True,text=None)
                raise
        return asyncio.run(turn(req))


if __name__=='__main__':
    try:print(json.dumps(dict(ok=True,result=operate(json.load(sys.stdin))),ensure_ascii=True,default=str))
    except Exception as e:
        print(json.dumps(dict(ok=False,error=type(e).__name__,phase=PHASE)))
        sys.exit(1)
