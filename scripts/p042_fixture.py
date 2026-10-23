"""Private T04.2 operator: scripted raw SDK; real application and persistence."""
from pathlib import Path
import asyncio,json,secrets,socket,sys,os,base64,hmac,time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import psycopg,uvicorn
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import JSONResponse
from evals.loan_world import LoanWorld
from scripts.p042_check import RawScript,serve_loans,timing_receipt,bounded_completion_capabilities
from store.banking_contracts import ProviderError
LOCAL=ROOT/'.genesis/local/t042-fixture'

CONTRACT='# T04.2 private operational access\nRead access.json privately. Never print secrets, credentials, cookies, CSRF, opaque cursor values or DSNs. No implementation/maker outcomes are supplied. Only raw SDK output is scripted; real PKCE/auth, public TLS API, worker, provider SQL and rendering stay real. Bank domain date Jan15 is fixed; session expiry, deadlines and source-update time remain real.\n\nPublic API: GET /api/v1/auth/login -> real Keycloak form/PKCE, registered callback https://localhost:3443/bank-api/auth/callback. Deliver original callback query + genuine flow cookie to api_base/api/v1/auth/callback. Session GET /api/v1/auth/session returns customer_id/csrf_token. Origin + X-CSRF-Token required for mutation routes. POST /api/v1/conversations {} ->id. POST /api/v1/conversations/{id}/turns {client_turn_id:canonicalUUID,text,instructions?:{language:en|hi,format:concise|detailed}} ->202 status_url/events_url. Status JSON has result containing text,evidence,next_question? etc once terminal. Public durable turn key replay/409, status/SSE owner/session controls apply. Loan ID and optional installment ID must occur explicitly in current question. No identity or mutation tools.\n\nRaw SDK plan {tool,args,draft?}. Tools: list_loans(limit?,cursor?), get_loan(loan_id), list_emi_schedule(loan_id,start?,end?,limit?,cursor?), list_loan_payments(loan_id,installment_id?,start?,end?,status?:posted|pending|declined|reversed,limit?,cursor?). Dates ISOYYYY-MM-DD inclusive UTC, page size1..100, range0..365 days. unsupported/clarify args{}. Draft {text?,claims?:[{ref,field,value}]}; unchecked prose never bank authority. Actual empty payment page has explicit coverage and never confirms payment. Existing account/card reads also available.\n\nGET /oracle actual tuple orders:\nloans: id,customer_id,name,status,currency,principal_minor,apr_percent,monthly_payment_minor,version,as_of.\nemi_schedule: id,loan_id,customer_id,installment_number,due_on,scheduled_minor,version,as_of.\nloan_payments: id,loan_id,customer_id,installment_id,amount_minor,status,occurred_at,reason,version,as_of.\nMoney minor units and APR are exact strings. A schedule is not payment evidence; partial/pending/declined/reversed records do not establish fully paid. Unknown reasons must stay unknown.\n\nPrivate controls require X-Fixture-Secret:\nGET /state, /identity (actual matched containers), /oracle, /timing/{turn_id}.\nPOST /script {outputs:[rawplan|string,...],error?:bool,delay?:seconds} resets raw SDK output only; last output repeats. No paid calls.\nPOST /pause {paused:bool}; /flag {enabled:bool} controls loan lane only, accounts/cards stay available.\nPOST /loan-lock {locked:bool} holds actual ACCESS EXCLUSIVE banking.loans lock, release afterward. Never query oracle while held.\nPOST /loan-change {loan_id,monthly_payment_minor} privileged synthetic-bank update limited to fixture loan ID. Source triggers remain real.\nPOST /provider {cookie:rawSIDstring,operation:registeredread,args:{...}} derives context from actual existing session and calls real provider. Label direct-adapter probe, not public turn. cookie is ONLY the nonempty raw string value of __Host-bank_sid, never a Cookie header/object/Promise.\nPOST /expired-cursor {cookie,loan_id,start,end,limit:1} creates genuine already-expired signed payment cursor without changing any clock/key exposure.\nGET /permissions: actual runtime nonowner/nonbypass/RLS/no-context/no-writes checks.\nPOST /expire-session {cookie:rawSIDstring,milliseconds:40} changes actual Redis TTL for that session.\nPOST /stop-api then /reset (409 if running); POST /start-api (reread access.json). Reset is guarded owned private DB only; do not reset normal/demo/original data.\n\nBrowser https://localhost:3443 is actual frontend. Use isolated context ignoreHTTPSErrors:true for self-signed local only. Forward ALL /bank-api/** (query, original method/body, Cookie/Origin/CSRF) to api_base/api/v1/**, with upstream maxRedirects:0 so browser follows real auth redirects. CDP Fetch interception works when page routing misses callbacks. Do not rewrite /_next or frontend content. Set raw plan before each user question/button; for continuation use exact next_question/cursor from the same rendered turn status/history, not a separate provider cursor. Use a new conversation to avoid stale pagination buttons. Do not make ordinary/demo model questions. Browser forwarding may buffer SSE; do not claim browser incremental timing.\n\nLifecycle: process.json identifies private operator. Keep alive for verification; root cleans only this owned process/session namespaces, retains SQL/evidence. Read only behavioral cases/these operator files/tool docs; no app/fixture source, maker tests/results or old chat. Report failures/partial coverage honestly, no approval/status changes.\n'

class Operator:
    def __init__(self):
        self.config=json.loads((ROOT/'.genesis/local/p01/config.json').read_text());self.world=LoanWorld.create(self.config)
        self.script=RawScript();self.manager=None;self.app=None;self.base=None;self.lock=None
    async def start(self):
        self.manager=serve_loans(self.world,self.script);self.app,self.base=await self.manager.__aenter__();self.app.state.turn_worker.paused=False
    async def stop(self):
        if self.lock:self.lock.rollback();self.lock.close();self.lock=None
        if self.manager:await self.manager.__aexit__(None,None,None)
        self.manager=None;self.app=None;self.base=None
    def save(self,control,secret):
        LOCAL.mkdir(parents=True,exist_ok=True)
        value=dict(api_base=self.base,control_base=control,control_secret=secret,origin=self.world.settings.origin,ca_file=self.world.settings.oidc_ca_file,
            database=self.world.name,namespace=self.world.namespace,domain_time=self.world.fixture['domain_time'],cards=self.world.fixture['cards'],transactions=self.world.fixture['card_transactions'],
            accounts=self.world.fixture['accounts'],loans=self.world.fixture['loans'],emi_schedule=self.world.fixture['emi_schedule'],loan_payments=self.world.fixture['loan_payments'],users=[dict(label=label,username=u['username'],password=u['password'],customer_id=self.world.fixture['customers'][i]) for i,(label,u) in enumerate(zip(['A','B'],self.config['users']))])
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
    async def state():return dict(database=op.world.name,namespace=op.world.namespace,api_running=bool(op.manager),api_base=op.base,domain_time=op.world.fixture['domain_time'],sql_lock=bool(op.lock),raw_dispatches=len(op.script.calls),loans_enabled=op.app.state.turn_worker.provider.loans_enabled if op.app else None)
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
    async def flag(r:Request):op.app.state.turn_worker.provider.loans_enabled=bool((await r.json())['enabled']);return dict(enabled=op.app.state.turn_worker.provider.loans_enabled)
    @api.post('/loan-lock')
    async def lock(r:Request):
        if (await r.json())['locked']:
            if not op.lock:op.lock=psycopg.connect(op.world.admin_dsn);op.lock.execute('LOCK TABLE banking.loans IN ACCESS EXCLUSIVE MODE')
        elif op.lock:op.lock.rollback();op.lock.close();op.lock=None
        return dict(locked=bool(op.lock))
    @api.post('/loan-change')
    async def change(r:Request):
        x=await r.json()
        if x['loan_id'] not in {v['id'] for v in op.world.fixture['loans']}:raise HTTPException(422)
        with psycopg.connect(op.world.admin_dsn) as c:c.execute('UPDATE banking.loans SET monthly_payment_minor=%s WHERE id=%s',(int(x['monthly_payment_minor']),x['loan_id']))
        return dict(changed=True)
    async def context(x):return await op.app.state.sessions.require(x['cookie'])
    @api.post('/provider')
    async def provider(r:Request):
        x=await r.json()
        if x['operation'] not in ['list_loans','get_loan','list_emi_schedule','list_loan_payments','get_card','list_cards','get_account']:raise HTTPException(422)
        try:return (await getattr(op.app.state.turn_worker.provider,x['operation'])(await context(x),**x.get('args',{}))).model_dump(mode='json')
        except ProviderError as e:return JSONResponse(dict(code=e.code,retryable=e.retryable),status_code=e.status)
    @api.post('/expired-cursor')
    async def expired(r:Request):
        x=await r.json();p=op.app.state.turn_worker.provider;args={k:v for k,v in x.items() if k!='cookie'};page=await p.list_loan_payments(await context(x),**args)
        if not page.next_cursor:raise HTTPException(422)
        body=page.next_cursor.split('.')[0];v=json.loads(base64.urlsafe_b64decode(body+'='*(-len(body)%4)));v['exp']=int(time.time())-1
        body=base64.urlsafe_b64encode(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).decode().rstrip('=');sig=base64.urlsafe_b64encode(hmac.digest(p._key,body.encode(),'sha256')).decode().rstrip('=');return dict(cursor=body+'.'+sig)
    @api.get('/permissions')
    async def permissions():
        with psycopg.connect(op.world.settings.runtime_dsn) as c:
            role=c.execute('SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
            rows={t:c.execute('SELECT count(*) FROM banking.'+t).fetchone()[0] for t in ['loans','emi_schedule','loan_payments']}
            rls=c.execute("SELECT relname,relrowsecurity,relforcerowsecurity,has_table_privilege(current_user,oid,'INSERT,UPDATE,DELETE') FROM pg_class WHERE oid IN ('banking.loans'::regclass,'banking.emi_schedule'::regclass,'banking.loan_payments'::regclass) ORDER BY relname").fetchall()
            try:c.execute('UPDATE banking.loans SET monthly_payment_minor=monthly_payment_minor');denied=False
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
