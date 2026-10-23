from dataclasses import replace
from pathlib import Path
import html
import json
import os
import re
import uuid
from urllib.parse import urlsplit
import httpx
import pytest
import pytest_asyncio
from auth.config import Settings
from api.foundation import create_app

ROOT=Path(__file__).resolve().parents[2]


class SecretConfig(dict):
    def __repr__(self):
        return '<P01 isolated configuration: credentials redacted>'


def protected(value):
    if isinstance(value,dict):
        return SecretConfig({k:protected(v) for k,v in value.items()})
    if isinstance(value,list): return [protected(v) for v in value]
    return value


@pytest.fixture
def config():
    # Missing actual services is setup failure, never an offline passing substitute.
    return protected(json.loads((ROOT/'.genesis/local/p01/config.json').read_text(encoding='utf-8')))


@pytest.fixture
def settings(config):
    e=config['env']
    return Settings(issuer=e['OIDC_ISSUER'],client_id=e['OIDC_CLIENT_ID'],client_secret=e['OIDC_CLIENT_SECRET'],
                    redirect_uri=e['OIDC_REDIRECT_URI'],origin=e['BANK_ORIGIN'],redis_url=e['BANK_REDIS_URL'],
                    runtime_dsn=e['BANK_DATABASE_URL'],identity_dsn=e['BANK_AUTH_DATABASE_URL'],
                    namespace='p01test_'+uuid.uuid4().hex,oidc_ca_file=e.get('OIDC_CA_FILE'),
                    worker_dsn=config['dsns']['worker'])


@pytest_asyncio.fixture
async def app(settings):
    application=create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def client(app,settings):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=settings.origin) as c:
        yield c


async def login(client,user):
    start=await client.get('/api/v1/auth/login')
    assert start.status_code==303
    location=start.headers['location']
    assert 'code_challenge_method=S256' in location
    async with httpx.AsyncClient(follow_redirects=False,timeout=10,verify=str(ROOT/'.genesis/local/p01/tls.crt')) as issuer:
        form=await issuer.get(location)
        action=re.search(r'<form[^>]+action="([^"]+)"',form.text)
        assert action is not None, 'Actual test issuer did not return a login form'
        result=await issuer.post(html.unescape(action[1]),data={
                'username':user['username'],'password':user['password'],'credentialId':''})
        assert result.status_code in (302,303), 'Actual issuer login failed'
    query=urlsplit(result.headers['location']).query
    callback='/api/v1/auth/callback?'+query
    response=await client.get(callback)
    assert response.status_code==303, 'Code exchange/callback failed'
    current=await client.get('/api/v1/auth/session')
    assert current.status_code==200
    assert current.json()['customer_id']==user['customer']
    return current.json(),response,callback


def mutation_headers(settings,session):
    return {'Origin':settings.origin,'X-CSRF-Token':session['csrf_token']}


def observation(name,data):
    destination=os.environ.get('P01_EVIDENCE_DIR')
    if destination:
        folder=Path(destination)
        folder.mkdir(parents=True,exist_ok=True)
        (folder/name).write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')
