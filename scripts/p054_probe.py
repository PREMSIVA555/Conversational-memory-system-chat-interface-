"""Fresh behavioral access to the integrated journey. Private clocks/leases only."""
import asyncio,json,sys
from pathlib import Path
from datetime import datetime,timedelta
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import psycopg
from scripts.p053_probe import ContinuityProbe
from scripts.p052_probe import ConfirmationProbe
from evals.journey_world import JourneyWorld
from evals.banking_world import database_dsn
from evals.world import DomainClock
from api.preferences import Capture
from jobs.banking_consumer import Consumer

class JourneyProbe(ContinuityProbe):
    async def start(self):
        await ConfirmationProbe.start(self,world_class=JourneyWorld,continuity_enabled=True)
        self.clock=DomainClock(datetime.fromisoformat(self.world.fixture['domain_time']))
        self.app.state.turn_worker.provider._clock=self.clock.now
        self.app.state.turn_worker.provider.policies_enabled=True
        from scripts.p053_upgrade import snapshot
        self.financial_config={**self.world.config,'admin_dsn':self.world.admin_dsn}
        self.financial_before=snapshot(self.financial_config)
        return self

    async def step(self,step):
        s=self.resolve(step);op=s['op'];actor=s.get('actor','A');client=self.clients[actor]
        if op in ('policy_change','policy_restore'):
            from store.banking_policies import stage,activate
            from scripts.p054_policy import with_card_support
            import hashlib
            self.world.guard()
            with psycopg.connect(database_dsn(self.world.config['dsns']['migrator'],self.world.name),autocommit=True) as c:
                old,manifest=c.execute('SELECT s.id,s.manifest FROM banking.policy_active a JOIN banking.policy_sets s ON s.id=a.set_id WHERE a.singleton').fetchone()
                if op=='policy_restore':manifest=with_card_support(json.loads((ROOT/'evals/fixtures/p04_policy_queries.json').read_text(encoding='utf-8'))['manifest'])
                else:
                    doc=next(d for d in manifest['documents'] if d['version']==s.get('version','CARD-SUPPORT-v1'))
                    if 'text' in s:
                        doc['source']=s['text'];doc['source_hash']=hashlib.sha256(s['text'].encode()).hexdigest();doc['chunks']=[dict(section='procedure',text=s['text'],embedding=[0,0,1])];doc['expected_chunks']=1
                    for key in ('product','scope','locale','effective_from','effective_until'):
                        if key in s:doc[key]=s[key]
                new=stage(c,manifest);activate(c,new,expected=old)
            return {'private_policy_set':new,'ordinary_demo_changed':False}
        if op=='prepare':
            r=await client.post('/api/v1/workspace/prepare',json={})
        elif op=='message':
            r=await client.post(f'/api/v1/conversations/{s["cid"]}/messages',json={'client_turn_id':str(uuid4()),'workspace':True,'text':s['text'] if 'text' in s else ''.join(s['text_parts']),**s.get('fields',{})})
        elif op=='policy_read':
            from dataclasses import asdict
            from store.banking_contracts import ProviderError
            try:return {'production_service_read':True,'body':asdict(await self.app.state.turn_worker.provider.explain_policy(self.contexts[actor],**s['body']))}
            except ProviderError as error:return {'production_service_read':True,'status':error.status,'error':error.code}
        elif op=='work':return {'worked':await self.app.state.turn_worker.one()}
        elif op=='result':
            r=await client.get(f'/api/v1/conversations/{s["cid"]}/turns/{s["id"]}')
        elif op=='preference_accept':
            # Deliberately hold production accepted work before dispatch. No direct
            # projection writes and no HTTP/background auto-dispatch race.
            value=await self.app.state.preferences.accept(self.contexts[actor],Capture(client_event_id=uuid4(),text=s['text']))
            if s.get('save'):self.saved[s['save']]=value
            return {'fixture_held_accepted_work':True,'body':value}
        elif op=='preference_process':
            consumer=Consumer('',database_dsn(self.world.config['dsns']['worker'],self.world.name))
            return {'dispatch':await asyncio.to_thread(consumer.run_preference,self.contexts[actor].customer_id,s['event'])}
        elif op=='preferences':r=await client.get('/api/v1/preferences')
        elif op=='new_session':
            owner=self.contexts[actor].customer_id
            await self.app.state.sessions.revoke(self.contexts[actor])
            token=await self.app.state.sessions.issue(owner)
            self.contexts[actor]=await self.app.state.sessions.require(token)
            client.headers.update({'Cookie':'__Host-bank_sid='+token,'X-CSRF-Token':self.contexts[actor].csrf})
            return {'server_issued_fresh_session':True}
        elif op=='next_day':
            self.clock=DomainClock(self.clock.now()+timedelta(days=1))
            self.app.state.turn_worker.provider._clock=self.clock.now
            self.world.guard()
            (self.world.folder/'domain-clock.json').write_text(json.dumps({'domain_time':self.clock.now().isoformat()}),encoding='utf-8')
            return {'domain_time':self.clock.now().isoformat(),'security_clock_changed':False,'elapsed_24h_claim':False}
        elif op=='oracle':
            value=await super().step(step)
            from scripts.p053_upgrade import snapshot
            value['financial_data_unchanged']=snapshot(self.financial_config)==self.financial_before
            with psycopg.connect(self.world.admin_dsn) as c:
                value['preference_events']=c.execute('SELECT source_id::text,status FROM banking.preference_proposals ORDER BY source_id').fetchall()
                value['language_projection']=c.execute("SELECT customer_id::text,value,last_sequence FROM banking.preferences_current WHERE key='preferred_response_language' ORDER BY customer_id").fetchall()
            value['domain_time']=self.clock.now().isoformat()
            return value
        else:return await super().step(step)
        value={'status':r.status_code,'body':r.json()}
        if s.get('save'):self.saved[s['save']]=value['body']
        return value

async def main():
    p=await JourneyProbe().start()
    try:
        body=json.loads(sys.stdin.read())
        print(json.dumps({'results':[await p.step(s) for s in body['steps']],'oracle':await p.step({'op':'oracle'})},default=str))
    finally:await p.close()
if __name__=='__main__':asyncio.run(main())
