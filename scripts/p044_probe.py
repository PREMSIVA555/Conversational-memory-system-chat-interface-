"""T04.4 guarded behavioral seams: pure claim rubric and real login/API turn.

Only SDK raw completion is scripted; accepted work, worker, SQL, budgets and
rendering use the actual app. No arbitrary DSN/SQL, no paid model calls.
"""
from pathlib import Path
import sys,json,asyncio,re,socket
from contextlib import asynccontextmanager
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import httpx,uvicorn
from api.foundation import create_app
from store.banking_policy_answers import explain
from graphs.account_coordinator import EvidenceRegistry,Draft
from evals.policy_world import PolicyWorld
from scripts.p043_probe import operate as catalog_operation
from scripts.p03_slice import RawScript,real_login,timing_receipt
from llm.config import bounded_completion_capabilities


def world_for(name):
    if type(name) is not str or not re.fullmatch(r'p03_slice_[a-f0-9]{32}',name):raise ValueError('Guarded world required')
    config=json.loads((ROOT/'.genesis/local/p01/config.json').read_text(encoding='utf-8'))
    m=json.loads((ROOT/'.genesis/local/p03-worlds'/name/'manifest.json').read_text())
    w=PolicyWorld(config,name,m['marker']);w.guard();return w


@asynccontextmanager
async def serve_policy(w,script,enabled=True):
    # Metadata readiness precedes acceptance; no completion/embedding is called.
    await bounded_completion_capabilities(timeout=10)
    app=create_app(w.settings,model_transport=script,turns_enabled=True,turn_worker_paused=True,
        turn_run_seconds=15,cards_enabled=True,loans_enabled=True,policies_enabled=enabled)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='critical',access_log=False,
        ssl_certfile=str(ROOT/'.genesis/local/p01/tls.crt'),ssl_keyfile=str(ROOT/'.genesis/local/p01/tls.key')))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(500):
        if server.started:break
        if task.done():await task
        await asyncio.sleep(.01)
    assert server.started
    try:yield app,f'https://127.0.0.1:{port}'
    finally:
        server.should_exit=True
        try:await asyncio.wait_for(task,8)
        except TimeoutError:task.cancel();await asyncio.gather(task,return_exceptions=True)


async def api_turn(w,req):
    script=RawScript();script.set(req['plan'])
    user=req.get('user',0)
    if type(user) is not int or user not in (0,1):raise ValueError('Synthetic user index required')
    async with serve_policy(w,script,req.get('enabled',True)) as (app,base):
        async with httpx.AsyncClient(base_url=base,verify=w.settings.oidc_ca_file,timeout=25) as client:
            await real_login(client,w.config['users'][user],w.fixture['customers'][user],w)
            try:
                conversation=await client.post('/api/v1/conversations',json={});assert conversation.status_code==201
                cid=conversation.json()['id'];payload=dict(client_turn_id=str(uuid4()),text=req['text'])
                if 'instructions' in req:payload['instructions']=req['instructions']
                accepted=await client.post(f'/api/v1/conversations/{cid}/turns',json=payload);assert accepted.status_code==202
                ref=accepted.json();again=await client.post(f'/api/v1/conversations/{cid}/turns',json=payload);assert again.status_code==202 and again.json()['turn_id']==ref['turn_id']
                app.state.turn_worker.paused=False
                async with asyncio.timeout(23):
                    while True:
                        response=await client.get(ref['status_url']);assert response.status_code==200
                        turn=response.json()
                        if turn['result'] is not None:break
                        await asyncio.sleep(.03)
                events=await client.get(ref['events_url']);assert events.status_code==200
                return dict(accepted=202,replay_same_turn=True,result=turn['result'],timing=timing_receipt(w,ref['turn_id']),
                    sse_terminal=('answer.ready' in events.text or 'turn.failed' in events.text),raw_dispatches=len(script.calls),login='actual OIDC PKCE and session; credentials omitted')
            finally:
                token=client.cookies.get('__Host-bank_sid')
                if token:await app.state.sessions.command('delete',app.state.sessions.key('session',token))


def operate(req):
    if req['op']=='rubric':
        answer=explain(req['retrieval'],req['topic']);registry=EvidenceRegistry();registry.render(answer)
        draft=Draft.model_validate(req['draft']) if req.get('draft') is not None else None
        return dict(text=answer.text,evidence=answer.evidence,metadata=answer.metadata,draft_accepted=registry.accepts(draft),scope='pure claim-support rubric, not a policy serving/authentication path')
    if req['op']=='turn':return asyncio.run(api_turn(world_for(req['world']),req))
    return catalog_operation(req)


if __name__=='__main__':
    try:print(json.dumps(dict(ok=True,result=operate(json.load(sys.stdin))),default=str))
    except Exception as e:
        print(json.dumps(dict(ok=False,error=type(e).__name__,sqlstate=getattr(e,'sqlstate',None))))
        sys.exit(1)
