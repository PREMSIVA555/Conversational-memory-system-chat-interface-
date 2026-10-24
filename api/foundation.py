"""P01 banking foundation. No client identity, bank products, turns or model calls."""
import asyncio
from contextlib import asynccontextmanager
import json
import secrets
import hashlib
import os
from uuid import uuid4
from urllib.parse import parse_qs

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse, Response
from pydantic import BaseModel, ConfigDict
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from starlette.exceptions import HTTPException as StarletteHTTPException

from auth.config import Settings
from auth.oidc import OIDC
from auth.sessions import Sessions, SID_COOKIE, FLOW_COOKIE
from store.banking import Database
from api.privacy import Telemetry, event, REQUESTS, ROUTES, METHODS,configure_security_logging

IDENTITY_FIELDS = frozenset({'subject_id','actor_id','customer_id'})


def cookie(response, name, value, lifetime):
    response.set_cookie(name, value, max_age=lifetime, secure=True, httponly=True,
                        samesite='lax', path='/')


class Boundary:
    def __init__(self, app, telemetry):
        self.app, self.telemetry = app, telemetry

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope,receive,send)
        scope.setdefault('state',{})['request_id'] = str(uuid4())
        headers = {k.decode().lower():v.decode() for k,v in scope['headers']}
        forged = IDENTITY_FIELDS.intersection(parse_qs(scope['query_string'].decode()))
        forged |= {k for k in headers if k in {'x-subject-id','x-actor-id','x-customer-id'}}
        if forged:
            response = JSONResponse({'detail':'Identity is established by the server'}, status_code=422)
            return await response(scope,receive,send)
        method = scope['method'] if scope['method'] in METHODS else 'OTHER'
        with self.telemetry.tracer.start_as_current_span('bank.request', record_exception=False,
                                                       set_status_on_exception=False) as span:
            started = False
            completed = False
            # Incoming traceparent/baggage is not extracted and cannot establish identity.
            async def safe_send(message):
                nonlocal started,completed
                if message['type']=='http.response.start':
                    started = True
                    route = getattr(scope.get('route'),'path','unknown')
                    route = route if route in ROUTES else 'unknown'
                    status = message['status']
                    outcome = 'ok' if status < 400 else 'denied' if status < 500 else 'error'
                    span.set_attributes({'http.route':route,'http.method':method,'outcome':outcome})
                    REQUESTS.labels(route,method,outcome).inc()
                    event('request',route,method,outcome)
                    message['headers'] += [(b'cache-control',b'no-store'),
                        (b'x-content-type-options',b'nosniff'),
                        (b'x-request-id',scope['state']['request_id'].encode())]
                if message['type']=='http.response.body' and not message.get('more_body',False):
                    completed = True
                await send(message)
            try:
                await self.app(scope,receive,safe_send)
            except Exception:
                event('failure')
                if not started:
                    response=JSONResponse({'detail':'Service unavailable',
                        'request_id':scope['state']['request_id']},status_code=503)
                    await response(scope,receive,safe_send)
                elif not completed:
                    await send({'type':'http.response.body','body':b'','more_body':False})


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')


def create_app(settings=None,*,model_transport=None,turns_enabled=None,turn_run_seconds=10,turn_worker_paused=False,cards_enabled=None,loans_enabled=None,policies_enabled=None,workspace_enabled=None):
    if workspace_enabled is None:workspace_enabled=os.getenv('BANK_WORKSPACE_ENABLED','false').lower()=='true'
    if turns_enabled is None:turns_enabled=os.getenv('BANK_TURNS_ENABLED','false').lower()=='true'
    configure_security_logging()
    telemetry = Telemetry()
    try:
        settings = settings or Settings.from_env()
        sessions, oidc = Sessions(settings), OIDC(settings)
        database = Database(settings,sessions)
    except (ValueError,TypeError):
        settings = sessions = oidc = database = None

    @asynccontextmanager
    async def lifespan(app):
        if database:
            await database.open()
        task=None
        if database and turns_enabled:
            task=asyncio.create_task(app.state.turn_worker.serve())
        try:
            yield
        finally:
            if task:
                app.state.turn_worker.stopping.set()
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
            if database:
                await database.close()
                await sessions.close()
            telemetry.close()

    app = FastAPI(title='Banking foundation', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings, app.state.sessions, app.state.database = settings,sessions,database
    app.state.oidc, app.state.telemetry = oidc,telemetry
    app.add_middleware(Boundary,telemetry=telemetry)

    async def safe_error(request, status):
        labels={401:'Authentication required',403:'Request forbidden',404:'Not found',
                409:'Request conflict',422:'Invalid request',503:'Service unavailable'}
        return JSONResponse({'detail':labels.get(status,'Request failed'),
            'request_id':getattr(request.state,'request_id',str(uuid4()))},status_code=status)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request,exc):
        return await safe_error(request,exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc):
        return await safe_error(request,422)

    @app.exception_handler(Exception)
    async def unexpected_error(request,exc):
        event('failure')
        return await safe_error(request,503)

    def configured():
        if not settings:
            raise HTTPException(503,'Banking is not configured')

    async def principal(request: Request):
        configured()
        context = await sessions.require(request.cookies.get(SID_COOKIE))
        # A role/grant failure closes the serving path, not just the readiness probe.
        await database.ready()
        request.state.principal = context
        return context

    async def mutation(request: Request, context=Depends(principal)):
        if (request.headers.get('origin') != settings.origin or
            not secrets.compare_digest(request.headers.get('x-csrf-token',''),context.csrf)):
            raise HTTPException(403,'CSRF validation failed')
        return context

    @app.get('/health/live')
    async def live():
        return {'status':'alive'}

    @app.get('/health/ready')
    @app.get('/health',include_in_schema=False)
    async def ready():
        configured()
        try:
            await asyncio.wait_for(asyncio.gather(sessions.command('ping'),database.ready(),oidc.metadata()),8)
        except Exception:
            raise HTTPException(503,'Dependency readiness failed') from None
        return {'status':'ready','authentication':True,'database':True}

    @app.get('/metrics')
    async def metrics():
        return Response(generate_latest(),media_type=CONTENT_TYPE_LATEST)

    @app.get('/api/v1/auth/login')
    async def login():
        configured()
        try:
            await database.ready()
            state,browser,record = await sessions.begin()
            url = await oidc.authorization_url(state,record)
        except Exception:
            raise HTTPException(503,'Login unavailable') from None
        response=RedirectResponse(url,status_code=303)
        cookie(response,FLOW_COOKIE,browser,300)
        return response

    @app.get('/api/v1/auth/callback')
    async def callback(request:Request):
        configured()
        record = await sessions.consume(request.query_params.get('state'),request.cookies.get(FLOW_COOKIE))
        if not request.query_params.get('code') or request.query_params.get('error'):
            raise HTTPException(401,'Login failed')
        subject = await oidc.exchange(request.query_params['code'],record)
        customer = await database.map_identity(settings.issuer,subject)
        token = await sessions.issue(customer,request.cookies.get(SID_COOKIE))
        response=RedirectResponse(settings.origin,status_code=303)
        cookie(response,SID_COOKIE,token,settings.session_seconds)
        cookie(response,FLOW_COOKIE,'',0)
        return response

    @app.get('/api/v1/auth/session')
    async def current(context=Depends(principal)):
        return {'customer_id':context.customer_id,'csrf_token':context.csrf,'expires_at':context.expires_at}

    async def clear_resource_context(context):
        if not workspace_enabled:return
        # Redis revocation/rotation must remain available during a bank DB
        # outage. Failed SQL cleanup is inaccessible under the revoked binding
        # and expires; it must not keep an accepting login alive.
        try:
            from store.banking_preferences import binding
            async with asyncio.timeout(1):
                async with database.session(context) as c:await c.execute('SELECT banking.clear_workspace(%s)',(binding(context),))
        except Exception:event('failure')

    @app.post('/api/v1/auth/rotate')
    async def rotate(body:EmptyRequest,context=Depends(mutation)):
        await clear_resource_context(context)
        token=await sessions.rotate(context)
        response=JSONResponse({'status':'rotated'})
        cookie(response,SID_COOKIE,token,settings.session_seconds)
        return response

    @app.post('/api/v1/auth/logout')
    async def logout(body:EmptyRequest,context=Depends(mutation)):
        await clear_resource_context(context)
        await sessions.revoke(context)
        response=JSONResponse({'status':'logged-out'},headers={'Clear-Site-Data':'"cache", "storage"'})
        cookie(response,SID_COOKIE,'',0)
        return response

    @app.get('/api/v1/auth/watch')
    async def watch(context=Depends(principal)):
        async def heartbeat():
            while True:
                try:
                    await sessions.revalidate(context)
                except HTTPException:
                    return
                yield 'event: session\ndata: active\n\n'
                await asyncio.sleep(.25)
        return StreamingResponse(heartbeat(),media_type='text/event-stream')

    @app.get('/api/v1/conversations')
    async def conversations(context=Depends(principal)):
        return await database.list_conversations(context)

    @app.post('/api/v1/conversations',status_code=201)
    async def create(body:EmptyRequest,context=Depends(mutation)):
        return await database.create_conversation(context)

    @app.get('/api/v1/conversations/{resource_id}')
    async def conversation(resource_id:str,context=Depends(principal)):
        return await database.conversation(context,resource_id)

    from api.preferences import install
    install(app,database,settings,principal,mutation)
    from api.context_memory import install as install_context_memory
    install_context_memory(app,database,principal,mutation)
    from api.turns import install as install_turns
    install_turns(app,database,principal,mutation,enabled=turns_enabled,run_seconds=turn_run_seconds,workspace_enabled=workspace_enabled)
    if database:
        from store.banking_provider import AccountProvider
        from jobs.banking_turns import TurnWorker
        provider=AccountProvider(database,hashlib.sha256((settings.client_secret+'account-cursor-v1').encode()).digest(),
            cards_enabled=os.getenv('BANK_CARDS_ENABLED','false').lower()=='true' if cards_enabled is None else cards_enabled,
            loans_enabled=os.getenv('BANK_LOANS_ENABLED','false').lower()=='true' if loans_enabled is None else loans_enabled,
            policies_enabled=os.getenv('BANK_POLICIES_ENABLED','false').lower()=='true' if policies_enabled is None else policies_enabled)
        app.state.turn_worker=TurnWorker(database,provider,model_transport,run_seconds=turn_run_seconds)
        app.state.turn_worker.paused=turn_worker_paused
        from api.workspace import install as install_workspace
        install_workspace(app,database,provider,settings,principal,mutation,enabled=workspace_enabled)
    return app
