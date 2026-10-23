"""Private T04.1 operator: scripted raw SDK; real application and persistence."""
from pathlib import Path
import asyncio,json,secrets,socket,sys,os,base64,hmac,time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import psycopg,uvicorn
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import JSONResponse
from evals.card_world import CardWorld
from scripts.p04_check import RawScript,serve_cards,timing_receipt,bounded_completion_capabilities
from store.banking_contracts import ProviderError
LOCAL=ROOT/'.genesis/local/t041-fixture'

CONTRACT='''# T04.1 private operational access
Read access.json privately; never print passwords, control secret, session cookies, CSRF or DSNs. No implementation/maker results are provided. Only raw model SDK output is scripted. Auth, actual public TLS API, durable worker, provider SQL, evidence and persistence are real. Domain date is fixed Jan15; security expiry/deadlines are actual time.

access.json contains api_base, control_base, control_secret, origin, ca_file, users A/B, fixed fixture cards/transactions and domain date. identity.json reports matched actual PG16/Redis7 identities. Trial DB only, never shared/demo/original resets.

Public API: GET /api/v1/auth/login -> real Keycloak form (PKCE S256); submit actual username/password; registered callback remains https://localhost:3443/bank-api/auth/callback. Deliver unchanged query and genuine flow cookie to private api_base/api/v1/auth/callback. GET /api/v1/auth/session yields customer_id/csrf_token. Mutations require Origin and X-CSRF-Token. POST /api/v1/conversations {} ->id. POST /api/v1/conversations/{id}/turns {client_turn_id:canonicalUUID,text:string,instructions?:{language:en|hi,format:concise|detailed}} ->202 status_url/events_url. Same key+payload replays, changed payload409. Status/SSE owner/session bound. Card canonical ID must occur explicitly in current question; suffix alone needs clarification. Model registry permits no mutations.

Raw JSON plans: {tool,args,draft?}. Card tools list_cards args={last_four?:four digits,limit?:1..100,cursor?:opaque}; get_card args={card_id}; list_card_transactions args={card_id,start?:YYYY-MM-DD,end?:YYYY-MM-DD,status?:declined|pending|posted|reversed,limit?:1..100,cursor?:opaque}. unsupported/clarify args={}. Existing account tools unchanged. Draft is {text?:string,claims?:[{ref,field,value}]}: unsupported prose is not bank authority.

Card oracle tuple: id,customer_id,name,last_four,kind,status,currency,daily_limit_minor,credit_limit_minor,available_credit_minor,outstanding_minor,version,as_of. Transaction tuple: id,card_id,customer_id,amount_minor,status,occurred_at,reason,version,as_of. Native Money minor units remain integer strings. Credit fields of debit cards are NULL. UTC date range inclusive; order occurred_at then ID. Cursor is owner/card/range/status/page-size/version bound. Status output includes next_question when a card page has more data; actual frontend Load next page submits that question as a new durable turn.

Every operator call requires X-Fixture-Secret. These are private controls, not public application endpoints:
- GET /state: API state, actual domain date, dispatch count, card flag, SQL lock, database/namespace. GET /identity: matched live SQL/container and Redis/container identity.
- GET /oracle: actual read-only SQL bank oracle. Do not call while card-lock is held.
- GET /timing/{turn_id}: raw persisted events, attempts/usage and derived queue/run/full. Independently recompute from timestamps; missing usage is unknown.
- POST /script {outputs:[rawJSONobject-or-rawstring,...],error?:boolean,delay?:seconds}: raw completion only; last output repeats. error=true is raw provider outage. No paid dispatch.
- POST /pause {paused:boolean}: pause real worker claims, not acceptance.
- POST /card-lock {locked:boolean}: actual ACCESS EXCLUSIVE banking.cards lock in this trial DB; coordinate idle worker and always unlock.
- POST /card-change {card_id,daily_limit_minor}: actual privileged synthetic bank update on declared fixture card only; source triggers run normally.
- POST /flag {enabled:boolean}: trusted operator card rollout flag only; account path stays enabled.
- POST /provider {cookie:privateSID,operation:get_card|list_cards|list_card_transactions|get_account,args:{...}}: direct real adapter probe with context derived/revalidated from actual existing session. Returns real typed DTO or safe typed error. This is a labeled direct-provider probe, not the public turn API. No arbitrary SQL/identity override.
- POST /expired-cursor {cookie,card_id,start,end,limit:1}: creates a validly signed already-expired challenge from an actual first page without changing any clock; use to test actual provider/turn denial. Signing key is never exposed.
- GET /permissions: actual unscoped runtime card counts, role flags, forced RLS, write grants and attempted UPDATE denied (rolled back regardless).
- POST /expire-session {cookie,milliseconds:40}: actual Redis TTL on that trial session only.
- POST /stop-api then POST /reset: guarded owned-world reset refused409 while running. POST /start-api: actual app restarts, reread access.json and log in afresh.

Browser: actual existing frontend is https://localhost:3443. In an isolated browser context use ignoreHTTPSErrors:true only for this self-signed local certificate; no global trust change. Forward ALL /bank-api/** requests to api_base + /api/v1 + suffix, preserving query, method/body and genuine Cookie/Origin/CSRF headers. Fetch upstream with maxRedirects:0 so browser follows real Keycloak redirects/callback. Chrome DevTools Protocol Fetch interception can be used if page-level routing misses redirects. Frontend and /_next remain real existing UI. Configure raw plan through /script before each user question/button. No ordinary/demo API model questions.

Lifecycle: process.json identifies only this private operator. Keep alive until verification finished. Root cleans only fixture processes/sessions; SQL trial data/evidence retained. Do not read implementation, tests, maker outcomes or other Genesis docs. Report coverage/failures/limitations honestly; no approval/status edits.
'''

class Operator:
    def __init__(self):
        self.config=json.loads((ROOT/'.genesis/local/p01/config.json').read_text());self.world=CardWorld.create(self.config)
        self.script=RawScript();self.manager=None;self.app=None;self.base=None;self.lock=None
    async def start(self):
        self.manager=serve_cards(self.world,self.script);self.app,self.base=await self.manager.__aenter__();self.app.state.turn_worker.paused=False
    async def stop(self):
        if self.lock:self.lock.rollback();self.lock.close();self.lock=None
        if self.manager:await self.manager.__aexit__(None,None,None)
        self.manager=None;self.app=None;self.base=None
    def save(self,control,secret):
        LOCAL.mkdir(parents=True,exist_ok=True)
        value=dict(api_base=self.base,control_base=control,control_secret=secret,origin=self.world.settings.origin,ca_file=self.world.settings.oidc_ca_file,
            database=self.world.name,namespace=self.world.namespace,domain_time=self.world.fixture['domain_time'],cards=self.world.fixture['cards'],transactions=self.world.fixture['card_transactions'],
            accounts=self.world.fixture['accounts'],users=[dict(label=label,username=u['username'],password=u['password'],customer_id=self.world.fixture['customers'][i]) for i,(label,u) in enumerate(zip(['A','B'],self.config['users']))])
        (LOCAL/'access.json').write_text(json.dumps(value,indent=2));(LOCAL/'identity.json').write_text(json.dumps(self.world.identity(),indent=2));(LOCAL/'process.json').write_text(json.dumps(dict(pid=os.getpid(),database=self.world.name)));(LOCAL/'CONTRACT.md').write_text(CONTRACT,encoding='utf-8')

async def main():
    os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP','True');op=Operator();assert (await bounded_completion_capabilities(timeout=60))['structured_json'];await op.start()
    secret=secrets.token_urlsafe(32);sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];control=f'http://127.0.0.1:{port}';op.save(control,secret)
    api=FastAPI(docs_url=None,redoc_url=None)
    @api.middleware('http')
    async def guard(r,call_next):
        if not secrets.compare_digest(r.headers.get('X-Fixture-Secret',''),secret):return JSONResponse({'detail':'Forbidden'},status_code=403)
        return await call_next(r)
    @api.get('/state')
    async def state():return dict(database=op.world.name,namespace=op.world.namespace,api_running=bool(op.manager),api_base=op.base,domain_time=op.world.fixture['domain_time'],sql_lock=bool(op.lock),raw_dispatches=len(op.script.calls),cards_enabled=op.app.state.turn_worker.provider.cards_enabled if op.app else None)
    @api.get('/identity')
    async def identity():return await asyncio.to_thread(op.world.identity)
    @api.get('/oracle')
    async def oracle():return op.world.oracle()
    @api.get('/timing/{turn_id}')
    async def timing(turn_id:str):return timing_receipt(op.world,turn_id)
    @api.post('/script')
    async def script(r:Request):
        x=await r.json();op.script.set(*([RuntimeError('Synthetic provider unavailable')] if x.get('error') else x.get('outputs',[])),delay=min(20,max(0,float(x.get('delay',0)))));return dict(configured=True)
    @api.post('/pause')
    async def pause(r:Request):op.app.state.turn_worker.paused=bool((await r.json())['paused']);return dict(paused=op.app.state.turn_worker.paused)
    @api.post('/flag')
    async def flag(r:Request):op.app.state.turn_worker.provider.cards_enabled=bool((await r.json())['enabled']);return dict(enabled=op.app.state.turn_worker.provider.cards_enabled)
    @api.post('/card-lock')
    async def lock(r:Request):
        if (await r.json())['locked']:
            if not op.lock:op.lock=psycopg.connect(op.world.admin_dsn);op.lock.execute('LOCK TABLE banking.cards IN ACCESS EXCLUSIVE MODE')
        elif op.lock:op.lock.rollback();op.lock.close();op.lock=None
        return dict(locked=bool(op.lock))
    @api.post('/card-change')
    async def change(r:Request):
        x=await r.json()
        if x['card_id'] not in {v['id'] for v in op.world.fixture['cards']}:raise HTTPException(422)
        with psycopg.connect(op.world.admin_dsn) as c:c.execute('UPDATE banking.cards SET daily_limit_minor=%s WHERE id=%s',(int(x['daily_limit_minor']),x['card_id']))
        return dict(changed=True)
    async def context(x):return await op.app.state.sessions.require(x['cookie'])
    @api.post('/provider')
    async def provider(r:Request):
        x=await r.json()
        if x['operation'] not in ['get_card','list_cards','list_card_transactions','get_account']:raise HTTPException(422)
        try:return (await getattr(op.app.state.turn_worker.provider,x['operation'])(await context(x),**x.get('args',{}))).model_dump(mode='json')
        except ProviderError as e:return JSONResponse(dict(code=e.code,retryable=e.retryable),status_code=e.status)
    @api.post('/expired-cursor')
    async def expired(r:Request):
        x=await r.json();p=op.app.state.turn_worker.provider;args={k:v for k,v in x.items() if k!='cookie'};page=await p.list_card_transactions(await context(x),**args)
        if not page.next_cursor:raise HTTPException(422)
        body=page.next_cursor.split('.')[0];v=json.loads(base64.urlsafe_b64decode(body+'='*(-len(body)%4)));v['exp']=int(time.time())-1
        body=base64.urlsafe_b64encode(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).decode().rstrip('=');sig=base64.urlsafe_b64encode(hmac.digest(p._key,body.encode(),'sha256')).decode().rstrip('=');return dict(cursor=body+'.'+sig)
    @api.get('/permissions')
    async def permissions():
        with psycopg.connect(op.world.settings.runtime_dsn) as c:
            role=c.execute('SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
            rows={t:c.execute('SELECT count(*) FROM banking.'+t).fetchone()[0] for t in ['cards','card_transactions']}
            rls=c.execute("SELECT relname,relrowsecurity,relforcerowsecurity,has_table_privilege(current_user,oid,'INSERT,UPDATE,DELETE') FROM pg_class WHERE oid IN ('banking.cards'::regclass,'banking.card_transactions'::regclass) ORDER BY relname").fetchall()
            try:c.execute('UPDATE banking.cards SET daily_limit_minor=daily_limit_minor');denied=False
            except psycopg.errors.InsufficientPrivilege:denied=True
            finally:c.rollback()
        return dict(role=role,unscoped_rows=rows,rls=rls,runtime_update_denied=denied)
    @api.post('/expire-session')
    async def expire(r:Request):
        x=await r.json();changed=await op.app.state.sessions.command('pexpire',op.app.state.sessions.key('session',x['cookie']),max(1,min(5000,int(x.get('milliseconds',40)))));return dict(changed=bool(changed))
    @api.post('/stop-api')
    async def stop():await op.stop();op.save(control,secret);return dict(stopped=True)
    @api.post('/start-api')
    async def start():await op.start();op.save(control,secret);return dict(api_base=op.base)
    @api.post('/reset')
    async def reset():
        if op.manager:raise HTTPException(409)
        return dict(semantic_hash=await asyncio.to_thread(op.world.reset))
    server=uvicorn.Server(uvicorn.Config(api,host='127.0.0.1',port=port,log_level='critical',access_log=False))
    try:await server.serve(sockets=[sock])
    finally:await op.stop()
if __name__=='__main__':asyncio.run(main())
