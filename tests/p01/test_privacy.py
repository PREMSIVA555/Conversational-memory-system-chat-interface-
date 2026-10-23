import asyncio
import io
import json
import logging
import time
from dataclasses import replace
import httpx
import pytest
from fastapi import HTTPException
from api.foundation import create_app
from api.privacy import model_input,event,safe_log,DROPPED
from auth.sessions import SID_COOKIE
from jobs.banking_access import run_queued,AccessQueue,REJECTED,FAILED
from .conftest import login,mutation_headers,observation

CANARY='SYNTHETIC_SECRET_CANARY_f63cbbe2'


async def test_safe_errors_logs_spans_and_bounded_metric_labels(client,app,config):
    await login(client,config['users'][0])
    @app.get('/synthetic-fault/{raw}')
    async def fault(raw:str):
        raise RuntimeError(CANARY+' SQL password raw prompt')
    stream=io.StringIO(); handler=logging.StreamHandler(stream)
    safe_log.addHandler(handler)
    try:
        response=await client.get('/synthetic-fault/'+CANARY,headers={'traceparent':'00-'+'f'*32+'-'+'e'*16+'-01'})
        assert response.status_code==503 and CANARY not in response.text
        # Arbitrary event names, paths and values never enter operational metadata.
        event(CANARY,route=CANARY,outcome=CANARY)
        event('request',route=CANARY,method=CANARY,outcome=CANARY)
        app.state.telemetry.provider.force_flush()
        traces=list(app.state.telemetry.exporter.records)
        metrics=(await client.get('/metrics')).text
        assert CANARY not in stream.getvalue()+json.dumps(traces)+metrics
        assert all(t['trace_id']!='f'*32 for t in traces)
        assert 'customer_id' not in metrics and 'request_id=' not in metrics
        assert all('events' not in t for t in traces)
        observation('sink-scan.json',dict(canary_absent=True,
            tested_sinks=['public error','application JSON log','OTel export','Prometheus response'],
            rejected_remote_trace_id=True,operational_spans=traces))
    finally:
        safe_log.removeHandler(handler)


def test_pre_model_minimization(monkeypatch):
    monkeypatch.setenv('SYNTHETIC_API_KEY',CANARY)
    original=CANARY+' alice@example.invalid 4111 1111 1111 1111 Bearer synthetic-token'
    sanitized=model_input(original)
    assert CANARY not in sanitized and 'alice@' not in sanitized and '4111' not in sanitized and 'synthetic-token' not in sanitized


async def test_optional_export_failure_bounded_and_correlated_queued_span(client,app,config):
    await login(client,config['users'][0])
    ctx=await app.state.sessions.require(client.cookies.get(SID_COOKIE))
    with app.state.telemetry.tracer.start_as_current_span('bank.request') as span:
        parent=span.get_span_context()
        async def valid(context): return context.customer_id
        assert await run_queued(app.state.sessions,ctx,app.state.telemetry,parent,valid)==ctx.customer_id
    app.state.telemetry.provider.force_flush()
    records=list(app.state.telemetry.exporter.records)
    assert any(format(parent.trace_id,'032x') in r['links'] for r in records if r['name']=='bank.queued')
    before=DROPPED._value.get()
    def broken(record): raise RuntimeError(CANARY)
    app.state.telemetry.exporter.sink=broken
    started=time.monotonic()
    result=await client.get('/health/live')
    assert result.status_code==200 and time.monotonic()-started < 1
    app.state.telemetry.provider.force_flush()
    assert DROPPED._value.get()>before


async def test_missing_auth_configuration_liveness_but_closed_readiness(monkeypatch,settings):
    monkeypatch.delenv('OIDC_ISSUER',raising=False)
    app=create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=settings.origin) as c:
            assert (await c.get('/health/live')).status_code==200
            assert (await c.get('/health/ready')).status_code==503
            assert (await c.get('/api/v1/auth/login')).status_code==503
            assert (await c.get('/api/v1/auth/session')).status_code==503


async def test_bad_role_grants_fail_closed(client,app,config):
    import psycopg
    from psycopg import sql
    await login(client,config['users'][0])
    with psycopg.connect(config['admin_dsn'],autocommit=True) as admin:
        admin.execute(sql.SQL('GRANT SELECT ON banking.identity_links TO {}').format(sql.Identifier(config['roles']['runtime'])))
        try:
            assert (await client.get('/health/ready')).status_code==503
            assert (await client.get('/api/v1/conversations')).status_code==503
        finally:
            admin.execute(sql.SQL('REVOKE SELECT ON banking.identity_links FROM {}').format(sql.Identifier(config['roles']['runtime'])))


async def test_queue_overflow_and_invalid_session_alerts(client,app,config,settings):
    current,_,_=await login(client,config['users'][0])
    context=await app.state.sessions.require(client.cookies.get(SID_COOKIE))
    queue=AccessQueue(app.state.sessions,app.state.telemetry,capacity=1)
    called=[]
    async def operation(ctx): called.append(ctx.customer_id)
    with app.state.telemetry.tracer.start_as_current_span('bank.request') as span:
        parent=span.get_span_context()
    rejected,failed=REJECTED._value.get(),FAILED._value.get()
    assert queue.submit(context,parent,operation)
    assert not queue.submit(context,parent,operation)
    assert REJECTED._value.get()==rejected+1
    await client.post('/api/v1/auth/logout',json={},headers=mutation_headers(settings,current))
    await queue.drain_one()
    assert not called and queue.failures and FAILED._value.get()==failed+1


@pytest.mark.parametrize('change',[{'issuer':'http://evil.invalid'},
    {'origin':'http://localhost:3443'},{'redirect_uri':'https://evil.invalid/bank-api/auth/callback'},
    {'namespace':'../../outside'},{'session_seconds':0},{'client_secret':''}])
def test_invalid_security_configuration_rejected(settings,change):
    with pytest.raises(ValueError): replace(settings,**change)


async def test_library_error_and_access_logs_do_not_export_secrets(app):
    captured=io.StringIO()
    handler=logging.StreamHandler(captured)
    logger=logging.getLogger('uvicorn.error')
    logger.addHandler(handler)
    try:
        try: raise ValueError(CANARY)
        except ValueError: logger.error(CANARY,exc_info=True)
        assert CANARY not in captured.getvalue()
        assert logging.getLogger('uvicorn.access').disabled
        observation('library-log-scan.json',dict(canary_absent=True,access_logger_disabled=True))
    finally: logger.removeHandler(handler)


async def test_mandatory_audit_failure_rolls_back_conversation(client,settings,config):
    import psycopg
    current,_,_=await login(client,config['users'][0])
    with psycopg.connect(config['admin_dsn'],autocommit=True) as admin:
        before=admin.execute('SELECT count(*) FROM banking.conversations WHERE customer_id=%s',
                             (current['customer_id'],)).fetchone()[0]
        admin.execute("""CREATE FUNCTION banking.p01_test_audit_failure() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'SYNTHETIC_SECRET_CANARY_f63cbbe2'; END $$""")
        admin.execute('CREATE TRIGGER p01_test_audit_failure BEFORE INSERT ON banking.audit_events FOR EACH ROW EXECUTE FUNCTION banking.p01_test_audit_failure()')
        try:
            result=await client.post('/api/v1/conversations',json={},headers=mutation_headers(settings,current))
            assert result.status_code==503 and CANARY not in result.text
            after=admin.execute('SELECT count(*) FROM banking.conversations WHERE customer_id=%s',
                                (current['customer_id'],)).fetchone()[0]
            assert before==after
        finally:
            admin.execute('DROP TRIGGER p01_test_audit_failure ON banking.audit_events')
            admin.execute('DROP FUNCTION banking.p01_test_audit_failure()')


def test_prometheus_rules_fire_for_exercised_failure_counters():
    from pathlib import Path
    import subprocess
    root=Path(__file__).resolve().parents[2]
    result=subprocess.run(['docker','run','--rm','--network','none','--entrypoint','/bin/promtool',
        '--mount',f'type=bind,source={root / "infra"},target=/rules,readonly',
        '--mount',f'type=bind,source={root / "tests/p01/alerts.yml"},target=/alerts.yml,readonly',
        'prom/prometheus:v2.54.1','test','rules','/alerts.yml'],capture_output=True,text=True,timeout=20)
    assert result.returncode==0, 'Prometheus rule evaluation failed: '+result.stdout+result.stderr
