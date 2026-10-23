"""Test-only live fixture operator for independent black-box verification.

Private controls never run in the ordinary application. Public routes are the
actual application; the only model substitution is the raw SDK transport.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import secrets
import socket
import sys
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import psycopg
from fastapi import FastAPI,Request,HTTPException
import uvicorn
import redis
from evals.banking_world import BankingWorld,database_dsn
from scripts.p03_slice import RawScript,serve,timing_receipt

LOCAL=ROOT/'.genesis/local/t034-fixture'

class Operator:
    def __init__(self):
        self.config=json.loads((ROOT/'.genesis/local/p01/config.json').read_text(encoding='utf-8'))
        self.world=BankingWorld.create(self.config);self.script=RawScript();self.manager=None;self.app=None;self.base=None;self.lock=None
    async def start(self):
        if self.manager:raise ValueError('API already running')
        self.manager=serve(self.world,self.script)
        self.app,self.base=await self.manager.__aenter__()
        self.app.state.turn_worker.paused=False
    async def stop(self):
        if self.lock:self.lock.rollback();self.lock.close();self.lock=None
        if self.manager:await self.manager.__aexit__(None,None,None)
        self.manager=None;self.app=None;self.base=None
    def save(self,control,secret):
        LOCAL.mkdir(parents=True,exist_ok=True)
        obj=dict(api_base=self.base,control_base=control,control_secret=secret,origin=self.world.settings.origin,
            database=self.world.name,namespace=self.world.namespace,
            fixture_domain_time=self.world.fixture['domain_time'],users=[dict(label=x,username=u['username'],password=u['password'],customer_id=self.world.fixture['customers'][i]) for i,(x,u) in enumerate(zip(['A','B'],self.config['users']))],
            accounts=self.world.fixture['accounts'],transactions=self.world.fixture['transactions'],ca_file=self.world.settings.oidc_ca_file)
        (LOCAL/'access.json').write_text(json.dumps(obj,indent=2),encoding='utf-8')
        (LOCAL/'identity.json').write_text(json.dumps(self.world.identity(),indent=2),encoding='utf-8')
        (LOCAL/'process.json').write_text(json.dumps(dict(pid=os.getpid(),database=self.world.name,started_at=datetime.now(timezone.utc).isoformat())),encoding='utf-8')

async def main():
    global LOCAL
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--local-dir',type=Path,default=LOCAL);args=parser.parse_args()
    LOCAL=args.local_dir.resolve()
    if not LOCAL.is_relative_to((ROOT/'.genesis/local').resolve()) or LOCAL.is_symlink():raise ValueError('Fixture output must be inside private local workspace')
    os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP','True')
    op=Operator();secret=secrets.token_urlsafe(32)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];control=f'http://127.0.0.1:{port}'
    await op.start();op.save(control,secret)
    control_app=FastAPI(docs_url=None,redoc_url=None)
    @control_app.middleware('http')
    async def guard(request,call_next):
        if not secrets.compare_digest(request.headers.get('X-Fixture-Secret',''),secret):
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail':'Forbidden'},status_code=403)
        return await call_next(request)
    @control_app.get('/state')
    async def state():
        with redis.Redis.from_url(op.world.settings.redis_url,decode_responses=True) as r:cache=r.get(op.world.namespace+':canary')
        canary=op.world.folder/'canary.txt';file_value=canary.read_text(encoding='utf-8') if canary.exists() else None
        return dict(database=op.world.name,api_base=op.base,api_running=op.manager is not None,paused=op.app.state.turn_worker.paused if op.app else None,
            raw_model_dispatches=len(op.script.calls),sql_lock=op.lock is not None,semantic_hash=op.world.semantic_hash() if not op.lock else None,domain_time=op.world.fixture['domain_time'],real_utc=datetime.now(timezone.utc).isoformat(),redis_canary=cache,file_canary=file_value)
    @control_app.post('/canary')
    async def canary(request:Request):
        value=(await request.json())['value']
        if not isinstance(value,str) or len(value)>100:raise HTTPException(422)
        with redis.Redis.from_url(op.world.settings.redis_url) as r:r.set(op.world.namespace+':canary',value)
        (op.world.folder/'canary.txt').write_text(value,encoding='utf-8')
        return dict(written=True)
    @control_app.get('/oracle')
    async def oracle():return op.world.oracle()
    @control_app.get('/identity')
    async def identity():return await asyncio.to_thread(op.world.identity)
    @control_app.get('/timing/{turn_id}')
    async def timing(turn_id:str):return timing_receipt(op.world,turn_id)
    @control_app.post('/script')
    async def script(request:Request):
        value=await request.json();outputs=value.get('outputs',[])
        if value.get('error'):outputs=[RuntimeError('Synthetic raw provider unavailable')]
        op.script.set(*outputs,delay=min(20,max(0,float(value.get('delay',0)))))
        return dict(configured=True)
    @control_app.post('/pause')
    async def pause(request:Request):
        op.app.state.turn_worker.paused=bool((await request.json())['paused']);return dict(paused=op.app.state.turn_worker.paused)
    @control_app.post('/bank-lock')
    async def lock(request:Request):
        value=await request.json()
        if value['locked'] and not op.lock:
            op.lock=psycopg.connect(op.world.admin_dsn);op.lock.execute('LOCK TABLE banking.accounts IN ACCESS EXCLUSIVE MODE')
        elif not value['locked'] and op.lock:
            op.lock.rollback();op.lock.close();op.lock=None
        return dict(locked=op.lock is not None)
    @control_app.post('/bank-change')
    async def change(request:Request):
        value=await request.json();account=value['account_id']
        if account not in {a['id'] for a in op.world.fixture['accounts']}:raise HTTPException(422)
        with psycopg.connect(op.world.admin_dsn) as c:
            c.execute('UPDATE banking.accounts SET current_minor=%s,available_minor=%s WHERE id=%s',(int(value['current_minor']),int(value['available_minor']),account))
        return dict(changed=True)
    @control_app.post('/expire-session')
    async def expire(request:Request):
        value=await request.json();key=op.app.state.sessions.key('session',value['cookie'])
        changed=await op.app.state.sessions.command('pexpire',key,max(1,min(5000,int(value.get('milliseconds',40)))))
        return dict(changed=bool(changed))
    @control_app.post('/stop-api')
    async def stop():await op.stop();op.save(control,secret);return dict(stopped=True)
    @control_app.post('/start-api')
    async def start():await op.start();op.save(control,secret);return dict(api_base=op.base)
    @control_app.post('/reset')
    async def reset():
        if op.manager:raise HTTPException(409,'Stop actual API before resetting')
        value=await asyncio.to_thread(op.world.reset);return dict(semantic_hash=value)
    @control_app.post('/reset-negative')
    async def negative(request:Request):
        mode=(await request.json()).get('mode')
        from evals.banking_world import UnsafeTarget
        try:
            if mode=='runtime':op.world.reset(op.world.settings.runtime_dsn)
            elif mode=='shared':BankingWorld(op.config,'bank_p01',op.world.marker).reset()
            elif mode=='original':BankingWorld(op.config,'memory_system',op.world.marker).reset()
            else:raise HTTPException(422)
        except (UnsafeTarget,psycopg.Error) as e:return dict(refused=True,error_type=type(e).__name__)
        raise RuntimeError('Unsafe reset unexpectedly allowed')
    server=uvicorn.Server(uvicorn.Config(control_app,host='127.0.0.1',port=port,log_level='critical',access_log=False))
    try:await server.serve(sockets=[sock])
    finally:await op.stop()
if __name__=='__main__':asyncio.run(main())
