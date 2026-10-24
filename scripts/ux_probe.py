"""Private behavioral operator; no application imports this module."""
import asyncio,json,os,sys,time
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP','True')
import psycopg
from tests.ux.test_workspace import world,service,send
from store.banking_preferences import binding

class Probe:
    async def start(self):
        # Local SDK metadata/scanner setup is not a completion. Keep cold import
        # time outside each deliberately short durable application deadline.
        from llm.config import bounded_completion_capabilities
        from api.privacy import model_input
        await bounded_completion_capabilities(timeout=120)
        await asyncio.to_thread(model_input,'Synthetic banking enquiry')
        self.world=world.__wrapped__();self.generator=service.__wrapped__(self.world)
        self.app,self.clients,self.contexts,self.transport,_=await anext(self.generator)
        self.normal_session=self.app.state.database.session
        self.saved={};self.before=self.world.semantic_hash();return self
    async def close(self):await self.generator.aclose()
    def resolve(self,v):
        if isinstance(v,dict):return {k:self.resolve(x) for k,x in v.items()}
        if isinstance(v,list):return [self.resolve(x) for x in v]
        if isinstance(v,str) and v.startswith('$'):
            parts=v[1:].split('.');result=self.saved[parts[0]]
            for key in parts[1:]:result=result[int(key)] if isinstance(result,list) else result[key]
            return result
        return v
    async def step(self,step):
        s=self.resolve(step);op=s['op'];i={'A':0,'A2':1,'B':2}[s.get('actor','A')];c=self.clients[i]
        started=time.monotonic()
        if op=='parallel':return await asyncio.gather(*(self.step(x) for x in s['steps']))
        if op=='prepare':r=await c.post('/api/v1/workspace/prepare',json={})
        elif op=='products':r=await c.get('/api/v1/products/'+s['kind'],params=s.get('params',{}))
        elif op=='new':r=await c.post('/api/v1/workspace/new',json={})
        elif op=='turn':
            value=await send(c,s['cid'],s['text'],**s.get('fields',{}))
            result=dict(body=value,elapsed_ms=round((time.monotonic()-started)*1000,2),raw_dispatches=len(self.transport.calls))
            if s.get('save'):self.saved[s['save']]=value
            return result
        elif op=='accept':r=await c.post(f'/api/v1/conversations/{s["cid"]}/messages',json=s['body'],headers=s.get('headers',{}))
        elif op=='context':
            value=await self.app.state.workspace.context(self.contexts[i],s['cid'])
            if s.get('save'):self.saved[s['save']]=value
            return dict(body=value)
        elif op=='preferences':r=await c.get('/api/v1/preferences',params={'conversation_id':s['cid']} if s.get('cid') else {})
        elif op=='preference_messages':r=await c.get(f'/api/v1/conversations/{s["cid"]}/preference-messages')
        elif op=='request':r=await c.request(s.get('method','GET'),s['path'],json=s.get('body'),headers=s.get('headers',{}))
        elif op=='script':self.transport.outputs=s['outputs'];return {'configured':True}
        elif op=='revoke':await self.app.state.sessions.revoke(self.contexts[i]);return {'revoked':True}
        elif op=='bank_db_outage':
            from contextlib import asynccontextmanager
            @asynccontextmanager
            async def unavailable(*args,**kwargs):
                raise ConnectionError('Synthetic private bank DB access outage')
                yield
            self.app.state.database.session=unavailable;return {'bank_db_access':'unavailable','auth_store':'unchanged'}
        elif op=='restore_bank_db':self.app.state.database.session=self.normal_session;return {'bank_db_access':'restored'}
        elif op in ('expire_context','erase_context','change_product'):
            self.world.guard()
            with psycopg.connect(self.world.admin_dsn) as conn:
                if op=='expire_context':conn.execute("UPDATE banking.resource_context SET expires_at=clock_timestamp()-interval '1 second' WHERE session_binding=%s",(binding(self.contexts[i]),));return {'expired':True}
                if op=='change_product':
                    table={'account':'accounts','card':'cards','loan':'loans'}[s['kind']]
                    conn.execute(f'UPDATE banking.{table} SET version=version+1 WHERE id=%s',(s['id'],));return {'changed':True}
                source=conn.execute('SELECT source_id FROM banking.resource_context WHERE session_binding=%s AND conversation_id=%s',(binding(self.contexts[i]),s['cid'])).fetchone()[0]
            r=await c.delete('/api/v1/memory/sources/'+str(source))
        elif op=='oracle':
            return dict(identity=self.world.identity(),bank_data_unchanged=self.before==self.world.semantic_hash(),raw_dispatches=len(self.transport.calls))
        else:raise ValueError('Unknown operator')
        body=r.json();result={'status':r.status_code,'body':body,'elapsed_ms':round((time.monotonic()-started)*1000,2)}
        if s.get('save'):self.saved[s['save']]=body
        return result

async def main():
    p=await Probe().start()
    try:
        if '--serve' in sys.argv:
            # Short-lived private browser access. Credentials only enter ignored files.
            access=dict(base=str(p.clients[0].base_url).rstrip('/'),origin=p.world.settings.origin,
                actors={name:dict(cookie=c.headers['cookie'].split('=',1)[1]) for name,c in zip(['A','A2','B'],p.clients)})
            (ROOT/'.genesis/local/ux-browser-access.json').write_text(json.dumps(access),encoding='utf-8')
            print(json.dumps({'ready':True,'access':'.genesis/local/ux-browser-access.json'}),flush=True)
            await asyncio.Event().wait()
        else:
            req=json.loads(sys.stdin.read());results=[await p.step(s) for s in req['steps']]
            print(json.dumps(dict(results=results,oracle=await p.step({'op':'oracle'})),ensure_ascii=True,default=str))
    finally:await p.close()
if __name__=='__main__':asyncio.run(main())
