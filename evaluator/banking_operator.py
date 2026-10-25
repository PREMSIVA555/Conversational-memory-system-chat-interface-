"""External bridge to the preserved guarded banking operator, not shipped in wheel.

Only run from this application checkout with its private test configuration.
No normal-demo reset; JourneyProbe creates its own identity-verified world.
"""
import asyncio
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.p054_probe import JourneyProbe


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


async def main():
    body = json.loads(sys.stdin.read())
    if body.get('schema_version') != '1': raise ValueError('Unsupported operator protocol')
    p = await JourneyProbe().start()
    try:
        results = [await p.step(step) for step in body['steps']]
        oracle = await p.step({'op':'oracle'})
        sources = {}
        for directory in ('api','auth','capture','graphs','jobs','llm','store','scripts','evals'):
            for path in (ROOT/directory).rglob('*'):
                if path.is_file() and path.suffix in ('.py','.sql','.json'):
                    sources[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        sources['evaluator/banking_operator.py'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        fixture_hashes = {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'evals/fixtures').glob('*.json')}
        import psycopg
        with psycopg.connect(p.world.admin_dsn) as c:
            policy = c.execute('SELECT set_id FROM banking.policy_active WHERE singleton').fetchone()[0]
        environment = {'python':platform.python_version(),'platform':platform.platform(),
            'packages':{name:importlib.metadata.version(name) for name in ('psycopg','redis','httpx','fastapi','litellm')}}
        oracle['_source_manifest'] = sources
        oracle['_fixture_manifest'] = fixture_hashes
        oracle['_environment'] = environment
        print(json.dumps(dict(schema_version='1',results=results,oracle=oracle,
            identities=dict(source=digest(sources),data=digest(fixture_hashes),model='blocked-transport; attempts='+str(oracle['model_calls']),
                tools='production-services:'+digest(sources),policy=str(policy),environment=digest(environment)),
            reset=dict(namespace=p.world.name,semantic_hash=p.before,kind='guarded-postgres16-redis7')),default=str))
    finally: await p.close()


if __name__ == '__main__': asyncio.run(main())
