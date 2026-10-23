import asyncio
from dataclasses import replace
import json
import time
from urllib.parse import parse_qs,urlsplit
import httpx
import pytest
from auth.sessions import SID_COOKIE,FLOW_COOKIE
from auth.oidc import OIDC
from api.foundation import create_app
from jobs.banking_access import run_queued
from .conftest import login,mutation_headers


async def test_real_keycloak_login_cookie_and_callback_replay(client,config):
    session,response,callback=await login(client,config['users'][0])
    cookies=response.headers.get_list('set-cookie')
    sid=next(c for c in cookies if c.startswith(SID_COOKIE+'='))
    assert all(x in sid.lower() for x in ['secure','httponly','samesite=lax','path=/'])
    assert 'domain=' not in sid.lower()
    assert (await client.get(callback)).status_code==401


async def test_state_browser_binding_and_replay(client,app,settings):
    start=await client.get('/api/v1/auth/login')
    state=parse_qs(urlsplit(start.headers['location']).query)['state'][0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=settings.origin) as stranger:
        assert (await stranger.get('/api/v1/auth/callback',params={'state':state,'code':'unused'})).status_code==401
    assert (await client.get('/api/v1/auth/callback',params={'state':state,'code':'unused'})).status_code==401


async def test_pkce_verifier_mismatch_rejected(client,app,config):
    # Use a real authorization code but replace its verifier in the server-only flow.
    original=app.state.sessions.consume
    async def wrong(*args):
        record=await original(*args)
        record['verifier']='wrong-'*12
        return record
    app.state.sessions.consume=wrong
    with pytest.raises(AssertionError,match='Code exchange'):
        await login(client,config['users'][0])
    assert (await client.get('/api/v1/auth/session')).status_code==401


@pytest.mark.parametrize('field,value',[('iss','https://evil.invalid'),('aud','other-client'),
    ('nonce','wrong'),('exp',1),('sub','')])
def test_signed_token_claim_negatives(settings,field,value):
    from authlib.jose import JsonWebKey,JsonWebToken
    key=JsonWebKey.generate_key('RSA',2048,is_private=True,options={'kid':'test'})
    claims=dict(iss=settings.issuer,aud=settings.client_id,sub='subject',exp=int(time.time())+60,
                iat=int(time.time()),nonce='expected')
    claims[field]=value
    token=JsonWebToken(['RS256']).encode({'alg':'RS256','kid':'test'},claims,key).decode()
    with pytest.raises(Exception):
        OIDC(settings).validate_token({'id_token':token},{'keys':[key.as_dict(is_private=False)]},'expected')


def test_signature_and_unsigned_algorithm_rejected(settings):
    from authlib.jose import JsonWebKey,JsonWebToken
    key=JsonWebKey.generate_key('RSA',2048,is_private=True,options={'kid':'test'})
    other=JsonWebKey.generate_key('RSA',2048,is_private=True,options={'kid':'test'})
    claims=dict(iss=settings.issuer,aud=settings.client_id,sub='s',iat=int(time.time()),
                exp=int(time.time())+60,nonce='n')
    signed=JsonWebToken(['RS256']).encode({'alg':'RS256','kid':'test'},claims,key).decode()
    with pytest.raises(Exception): OIDC(settings).validate_token({'id_token':signed},{'keys':[other.as_dict()]},'n')
    with pytest.raises(Exception): OIDC(settings).validate_token({'id_token':'eyJhbGciOiJub25lIn0.e30.'},{'keys':[key.as_dict()]},'n')


async def test_rotation_revokes_old_cookie_and_old_csrf(client,app,settings,config):
    current,_,_=await login(client,config['users'][0])
    old=client.cookies.get(SID_COOKIE)
    rotated=await client.post('/api/v1/auth/rotate',json={},headers=mutation_headers(settings,current))
    assert rotated.status_code==200
    new=client.cookies.get(SID_COOKIE)
    assert new!=old
    with pytest.raises(Exception) as error: await app.state.sessions.require(old)
    assert error.value.status_code==401
    assert (await client.post('/api/v1/conversations',json={},headers=mutation_headers(settings,current))).status_code==403


@pytest.mark.parametrize('case',['missing','bad-origin-valid-csrf','missing-origin-valid-csrf','valid-origin-bad-csrf'])
async def test_csrf_origin_fail_closed(client,config,settings,case):
    current,_,_=await login(client,config['users'][0])
    headers=mutation_headers(settings,current)
    if case=='missing': headers={}
    elif case=='bad-origin-valid-csrf': headers['Origin']='https://evil.invalid'
    elif case=='missing-origin-valid-csrf': headers.pop('Origin')
    else: headers['X-CSRF-Token']='wrong'
    assert (await client.post('/api/v1/conversations',json={},headers=headers)).status_code==403


async def test_logout_rejects_queued_access_and_old_cookie(client,app,config,settings):
    current,_,_=await login(client,config['users'][0])
    context=await app.state.sessions.require(client.cookies.get(SID_COOKIE))
    called=[]
    async def operation(ctx): called.append(ctx.customer_id)
    with app.state.telemetry.tracer.start_as_current_span('bank.request') as span:
        parent=span.get_span_context()
    assert (await client.post('/api/v1/auth/logout',json={},headers=mutation_headers(settings,current))).status_code==200
    with pytest.raises(Exception) as error:
        await run_queued(app.state.sessions,context,app.state.telemetry,parent,operation)
    assert error.value.status_code==401 and not called
    assert (await client.get('/api/v1/auth/session')).status_code==401


async def test_expiry_uses_real_clock_and_stops_long_sse(settings,config):
    application=create_app(replace(settings,session_seconds=1))
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url=settings.origin) as c:
            await login(c,config['users'][0])
            started=time.monotonic()
            result=await c.get('/api/v1/auth/watch')
            assert result.status_code==200 and 'active' in result.text
            assert time.monotonic()-started < 2
            assert (await c.get('/api/v1/auth/session')).status_code==401


async def test_redis_failure_closes_access(client,app,config):
    await login(client,config['users'][0])
    from redis.exceptions import ConnectionError
    async def failed(*args,**kw): raise ConnectionError('SYNTHETIC_SECRET_CANARY')
    app.state.sessions.redis.get=failed
    response=await client.get('/api/v1/auth/session')
    assert response.status_code==503 and 'CANARY' not in response.text


async def test_legacy_routes_and_identity_overrides_disabled(client):
    for path in ['/chat','/memories/me','/memories/export']:
        assert (await client.get(path)).status_code==404
    assert (await client.post('/chat',json={'message':'x','subject_id':'forged'})).status_code==404
    assert (await client.get('/api/v1/conversations?customer_id=forged')).status_code==422
    assert (await client.get('/api/v1/conversations',headers={'X-Subject-Id':'forged'})).status_code==422
