"""JSON behavioral operator: disposable world, real HTTP and persistence; no model calls."""
import asyncio
import json
from pathlib import Path
import socket
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
import httpx
import psycopg
import uvicorn
from api.foundation import create_app
from evals.support_world import SupportWorld


class Probe:
    async def start(self, *, world_class=SupportWorld):
        self.world = world_class.create(json.loads((ROOT/'.genesis/local/p01/config.json').read_text(encoding='utf-8')))
        self.identity = self.world.identity()
        self.before = self.world.semantic_hash()
        self.model_calls = 0
        probe = self
        class NoPaidTransport:
            async def acompletion(self, **kwargs):
                probe.model_calls += 1
                raise RuntimeError('Paid model transport is disabled for T05.1 verification')
        self.app = create_app(self.world.settings, turns_enabled=True, turn_worker_paused=True, support_enabled=True,
                              workspace_enabled=True, cards_enabled=True, loans_enabled=True, model_transport=NoPaidTransport())
        sock = socket.socket(); sock.bind(('127.0.0.1', 0))
        self.url = f'http://127.0.0.1:{sock.getsockname()[1]}'
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level='critical', access_log=False))
        self.task = asyncio.create_task(self.server.serve(sockets=[sock]))
        for _ in range(1000):
            if self.server.started: break
            await asyncio.sleep(.01)
        if not self.server.started: raise RuntimeError('HTTP startup timeout')
        self.clients = {}; self.contexts = {}; self.saved = {}
        for actor, owner in [('A', 0), ('A2', 0), ('B', 1)]:
            token = await self.app.state.sessions.issue(self.world.fixture['customers'][owner])
            context = await self.app.state.sessions.require(token)
            self.contexts[actor] = context
            self.clients[actor] = httpx.AsyncClient(base_url=self.url, timeout=15, headers={
                'Cookie': '__Host-bank_sid='+token, 'Origin': self.world.settings.origin, 'X-CSRF-Token': context.csrf})
        for kind in ('accounts', 'cards', 'loans'):
            for actor, owner in [('a', 0), ('b', 1)]:
                self.saved[kind[:-1]+'_'+actor] = next(x['id'] for x in self.world.fixture[kind] if x['owner'] == owner)
        return self

    async def close(self):
        for name, client in self.clients.items():
            await client.aclose()
            await self.app.state.sessions.revoke(self.contexts[name])
        self.server.should_exit = True
        await asyncio.wait_for(self.task, 10)

    def resolve(self, value):
        if isinstance(value, dict): return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list): return [self.resolve(v) for v in value]
        if isinstance(value, str) and value.startswith('$'):
            parts = value[1:].split('.')
            result = self.saved[parts[0]]
            for part in parts[1:]: result = result[int(part)] if isinstance(result, list) else result[part]
            return result
        return value

    async def step(self, step):
        s = self.resolve(step); op = s['op']; actor = s.get('actor', 'A'); client = self.clients[actor]
        if op == 'parallel': return await asyncio.gather(*(self.step(x) for x in s['steps']))
        if op == 'turn':
            r = await client.post('/api/v1/conversations', json={}); r.raise_for_status()
            cid = r.json()['id']
            r = await client.post(f'/api/v1/conversations/{cid}/turns', json={'client_turn_id': str(uuid4()), 'text': s.get('text', 'Synthetic support question')})
        elif op == 'workspace_products':
            r = await client.get('/api/v1/products/'+s['kind'])
        elif op == 'workspace_turn':
            prepared = await client.post('/api/v1/workspace/prepare', json={}); prepared.raise_for_status()
            cid = prepared.json()['conversation_id']
            accepted = await client.post(f'/api/v1/conversations/{cid}/messages', json={
                'client_turn_id': str(uuid4()), 'workspace': True, 'text': s.get('text', 'Hi'), **s.get('fields', {})})
            accepted.raise_for_status()
            await self.app.state.turn_worker.one()
            r = await client.get(f'/api/v1/conversations/{cid}/turns/'+accepted.json()['turn_id'])
        elif op == 'revoke':
            await self.app.state.sessions.revoke(self.contexts[actor]); return {'revoked': actor}
        elif op == 'expire':
            # Test-only clock fault on a guarded private database; never an application capability.
            self.world.guard()
            with psycopg.connect(self.world.admin_dsn) as c:
                c.execute('ALTER TABLE banking.support_proposals DISABLE TRIGGER keep_support_proposal')
                c.execute("UPDATE banking.support_proposals SET created_at=clock_timestamp()-interval '20 minutes',expires_at=clock_timestamp()-interval '1 minute' WHERE id=%s", (s['id'],))
                c.execute('ALTER TABLE banking.support_proposals ENABLE TRIGGER keep_support_proposal')
            return {'expired': s['id']}
        elif op == 'erase':
            self.world.guard()
            with psycopg.connect(self.world.admin_dsn) as c:
                source = c.execute('SELECT source_id FROM banking.turns WHERE id=%s', (s['turn_id'],)).fetchone()[0]
            r = await client.delete('/api/v1/memory/sources/'+str(source))
        elif op == 'oracle':
            with psycopg.connect(self.world.admin_dsn) as c:
                rows = c.execute('SELECT id::text,source_turn_id::text,version,status,payload_hash FROM banking.support_proposals ORDER BY created_at,id').fetchall()
                cases = c.execute("SELECT to_regclass('banking.support_cases')").fetchone()[0]
            return {'proposals': rows, 'bank_data_unchanged': self.world.semantic_hash() == self.before, 'case_table': cases, 'identity': self.identity, 'model_calls': self.model_calls}
        elif op == 'model_plan':
            from graphs.account_coordinator import Plan
            from pydantic import ValidationError
            try: Plan.model_validate(s['body']); return {'accepted': True}
            except ValidationError: return {'accepted': False}
        else:
            base = '/api/v1/support/proposals'
            path = base if op == 'draft' else base+'/by-turn/'+s['turn_id'] if op == 'latest' else base+'/'+s['id']+('/'+op if op in ('edit', 'cancel', 'confirm') else '')
            headers = s.get('headers', {})
            r = await client.request('GET' if op in ('get', 'latest') else 'POST', path, json=s.get('body'), headers=headers)
        result = {'status': r.status_code, 'body': r.json()}
        if s.get('save'): self.saved[s['save']] = result['body']
        return result


async def main():
    request = json.loads(sys.stdin.read())
    probe = await Probe().start()
    try:
        results = [await probe.step(step) for step in request['steps']]
        print(json.dumps({'results': results, 'oracle': await probe.step({'op': 'oracle'})}, default=str))
    finally: await probe.close()


if __name__ == '__main__':
    asyncio.run(main())
