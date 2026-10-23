"""T02.5 real legacy SQL/capture + banking PG16/Redis cutover rehearsal."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
from itertools import count
import json
import os
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace
from uuid import uuid4
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest
from store.banking_cutover import inventory,install_guard,build_plan,apply_plan,digest
from store.banking_migrate import apply
from tests.p02.cutover_fixtures import create_legacy_fixture
from tests.p02.test_schema import connection,accept,project
from tests.p02.test_preferences import accepted
from tests.p01.conftest import settings,app,client,login,mutation_headers
from store.db import session as legacy_session,embedding_dim
from store import memories
from graphs.capture_state import Candidate

@pytest.fixture(scope='session')
def legacy(tmp_path_factory):
    fixture=create_legacy_fixture(protected_rows=40)
    with psycopg.connect(fixture.admin_dsn) as c:
        report=install_guard(c,inventory(c)['snapshot_sha256'],tmp_path_factory.mktemp('legacy-backup')/'rows.json')
    destination=os.getenv('P02_EVIDENCE_DIR')
    if destination:
        folder=Path(destination); folder.mkdir(parents=True,exist_ok=True)
        (folder/'legacy-identity-and-guard.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return fixture

@pytest.fixture(scope='session')
def ordinal(): return count()

@pytest.fixture
def legacy_row(legacy,ordinal): return legacy.records[next(ordinal)]

@pytest.fixture(autouse=True)
def migrated(config):
    with psycopg.connect(config['dsns']['migrator']) as c: apply(c,config['roles'],through='0016')

def mapping(config,customer,row,text='Always reply in English'):
    source,outbox=accepted(config,customer,text)
    with connection(config,customer,'migrator') as c:
        c.execute('INSERT INTO banking.legacy_owner_links(legacy_subject_id,customer_id,evidence_ref) VALUES(%s,%s,%s)',
            (row['legacy_subject_id'],customer,'synthetic explicit owner attestation'))
    return [dict(legacy_id=row['legacy_id'],customer_id=str(customer),source_id=str(source),value='en')],source,outbox

def rollout(config,enabled):
    with psycopg.connect(config['dsns']['migrator']) as c:
        version=c.execute('SELECT generation FROM banking.memory_rollout').fetchone()[0]
        return c.execute('SELECT banking.set_memory_rollout(%s,%s)',(enabled,version)).fetchone()[0]

def test_dry_run_classifies_all_legacy_rows_without_inventing_mapping(config,legacy):
    with psycopg.connect(legacy.admin_dsn) as old,psycopg.connect(config['dsns']['migrator']) as bank:
        before=inventory(old); plan=build_plan(old,bank,[])
        assert plan['input_rows']==plan['classified_rows']==40
        assert plan['counts']=={'missing_reviewed_owner_and_accepted_source':40}
        assert plan['writes']==0 and not plan['automatic_backfill']
        assert inventory(old)==before

def test_reviewed_backfill_uses_existing_source_and_replays_once(config,customer,legacy,legacy_row):
    mappings,source,outbox=mapping(config,customer,legacy_row)
    with psycopg.connect(legacy.admin_dsn) as old,connection(config,customer,'migrator') as bank:
        plan=build_plan(old,bank,mappings); proof=digest(plan)
        first=apply_plan(old,bank,mappings,proof); second=apply_plan(old,bank,mappings,proof)
        assert first['results']==[dict(legacy_id=legacy_row['legacy_id'],outcome='persisted')]
        assert second['results'][0]['outcome']=='duplicate'
        assert first['accepted_events_created']==first['legacy_rows_modified']==0
        assert bank.execute('SELECT count(*) FROM banking.accepted_user_events WHERE customer_id=%s',(customer,)).fetchone()[0]==1
        assert bank.execute('SELECT value,last_sequence FROM banking.preferences_current').fetchone()==('en',1)

def test_explicit_mapping_without_owner_link_stays_ambiguous(config,customer,legacy,legacy_row):
    source,_=accepted(config,customer,'Always reply in English')
    mappings=[dict(legacy_id=legacy_row['legacy_id'],customer_id=str(customer),source_id=str(source),value='en')]
    with psycopg.connect(legacy.admin_dsn) as old,connection(config,customer,'migrator') as bank:
        plan=build_plan(old,bank,mappings)
        assert plan['counts']['missing_reviewed_owner_link']==1
        assert apply_plan(old,bank,mappings,digest(plan))['attempted_rows']==0

def test_wrong_customer_accepted_source_is_not_backfilled(config,customer,legacy,legacy_row):
    mappings,source,_=mapping(config,customer,legacy_row)
    foreign=uuid4()
    with psycopg.connect(config['admin_dsn']) as c: c.execute('INSERT INTO banking.customers(customer_id) VALUES(%s)',(foreign,))
    other,_=accepted(config,foreign,'Always reply in English'); mappings[0]['source_id']=str(other)
    with psycopg.connect(legacy.admin_dsn) as old,connection(config,customer,'migrator') as bank:
        assert build_plan(old,bank,mappings)['counts']['accepted_source_owner_mismatch']==1

def test_exact_typed_source_and_review_hash_required(config,customer,legacy,legacy_row):
    mappings,source,_=mapping(config,customer,legacy_row)
    with psycopg.connect(legacy.admin_dsn) as old,connection(config,customer,'migrator') as bank:
        plan=build_plan(old,bank,mappings)
        with pytest.raises(ValueError): apply_plan(old,bank,mappings,'f'*64)
        mappings[0]['value']='hi'
        changed=build_plan(old,bank,mappings)
        assert changed['counts']['accepted_typed_source_mismatch']==1
        assert digest(plan)!=digest(changed)

def test_stale_legacy_backfill_cannot_replace_newer_value(config,customer,legacy,legacy_row):
    mappings,source,_=mapping(config,customer,legacy_row)
    with connection(config,customer) as c: newer=accept(c)
    with connection(config,customer,'worker') as c: assert project(c,newer,'hi')=='persisted'
    with psycopg.connect(legacy.admin_dsn) as old,connection(config,customer,'migrator') as bank:
        plan=build_plan(old,bank,mappings)
        assert apply_plan(old,bank,mappings,digest(plan))['results'][0]['outcome']=='ignored_stale'
        assert bank.execute('SELECT value,last_sequence FROM banking.preferences_current').fetchone()==('hi',newer[3])

def test_erased_accepted_source_is_not_backfilled(config,customer,legacy,legacy_row):
    mappings,source,_=mapping(config,customer,legacy_row)
    with connection(config,customer) as c: c.execute('SELECT banking.erase_memory_source(%s)',(source,))
    with psycopg.connect(legacy.admin_dsn) as old,connection(config,customer,'migrator') as bank:
        plan=build_plan(old,bank,mappings)
        assert plan['counts']['accepted_source_erased']==1
        assert apply_plan(old,bank,mappings,digest(plan))['attempted_rows']==0

@pytest.mark.parametrize('key',['preferred_response_language',' Preferred-Response-Language ','PREFERRED RESPONSE FORMAT'])
async def test_actual_capture_entry_point_permanently_ignores_protected_keys(legacy,legacy_row,key):
    result=await memories.persist_candidates(legacy_row['legacy_subject_id'],legacy_row['legacy_subject_id'],
        [Candidate(text='synthetic late English',attribute=key)])
    assert len(result)==1 and result[0]['reason']=='banking_preference_writer_retired'
    assert result[0]['action']=='ignored' and result[0]['memory_id'] is None

async def test_actual_capture_cannot_reinforce_retired_row_through_untyped_candidate(legacy,legacy_row,monkeypatch):
    @asynccontextmanager
    async def scoped(subject,actor):
        async with legacy_session(subject,actor,dsn=legacy.app_dsn) as c: yield c
    monkeypatch.setattr(memories,'session',scoped)
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        await memories.persist_candidates(legacy_row['legacy_subject_id'],legacy_row['legacy_subject_id'],
            [Candidate(text='synthetic generic echo',embedding=[1.0]+[0.0]*(embedding_dim()-1))])
    with psycopg.connect(legacy.admin_dsn) as c:
        assert c.execute('SELECT reinforcement_count FROM public.memories WHERE id=%s',(legacy_row['legacy_id'],)).fetchone()[0]==0

async def test_unrelated_legacy_capture_still_uses_real_sql_and_audit(legacy,monkeypatch):
    @asynccontextmanager
    async def scoped(subject,actor):
        async with legacy_session(subject,actor,dsn=legacy.app_dsn) as c: yield c
    monkeypatch.setattr(memories,'session',scoped); subject=str(uuid4())
    result=await memories.persist_candidates(subject,subject,[Candidate(text='synthetic unrelated fact')])
    assert result[0]['action']=='insert'
    with psycopg.connect(legacy.admin_dsn) as c:
        assert c.execute('SELECT count(*) FROM public.audit_log WHERE memory_id=%s',(result[0]['memory_id'],)).fetchone()[0]==1

@pytest.mark.parametrize('key',['preferred_response_language',' Preferred-Response-Format '])
def test_stale_old_app_sql_insert_is_fenced(legacy,key):
    with psycopg.connect(legacy.app_dsn) as c:
        customer=str(uuid4()); c.execute("SELECT set_config('app.subject_id',%s,true),set_config('app.actor_id',%s,true)",(customer,customer))
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            c.execute('INSERT INTO public.memories(subject_id,actor_id,content,attribute) VALUES(%s,%s,%s,%s)',(customer,customer,'synthetic stale',key))

def test_stale_old_app_sql_update_and_relabel_are_fenced(legacy,legacy_row):
    for query in ["UPDATE public.memories SET content='synthetic stale' WHERE id=%s",
                  "UPDATE public.memories SET attribute=NULL WHERE id=%s"]:
        with psycopg.connect(legacy.app_dsn) as c:
            subject=legacy_row['legacy_subject_id']; c.execute("SELECT set_config('app.subject_id',%s,true),set_config('app.actor_id',%s,true)",(subject,subject))
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState): c.execute(query,(legacy_row['legacy_id'],))

def test_runtime_and_worker_cannot_change_flags_or_mapping(config,customer):
    for role in ('runtime','worker','dispatcher','auth'):
        for statement,args in [('SELECT banking.set_memory_rollout(false,1)',()),
            ('SELECT banking.record_legacy_guard(%s)',(Jsonb({}),)),
            ('INSERT INTO banking.legacy_owner_links VALUES(%s,%s,%s)',(uuid4(),customer,'unauthorized'))]:
            with connection(config,customer,role) as c:
                with pytest.raises(psycopg.errors.InsufficientPrivilege): c.execute(statement,args)

def test_flag_cas_and_rollback_preserve_source_projection_tombstone(config,customer):
    source,outbox=accepted(config,customer,'Always reply in Hindi')
    with connection(config,customer) as c: c.execute('SELECT banking.erase_memory_source(%s)',(source,))
    try:
        disabled=rollout(config,False)
        with connection(config,customer) as c:
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState): accepted(config,customer,'Always reply in English')
        with psycopg.connect(config['dsns']['migrator']) as c:
            with pytest.raises(psycopg.errors.SerializationFailure): c.execute('SELECT banking.set_memory_rollout(true,%s)',(disabled['generation']-1,))
        with connection(config,customer) as c:
            assert c.execute('SELECT payload FROM banking.accepted_user_events WHERE id=%s',(source,)).fetchone()[0]=={}
            assert c.execute('SELECT count(*) FROM banking.erasure_ledger WHERE source_id=%s',(source,)).fetchone()[0]==1
    finally: rollout(config,True)

async def test_http_rollback_retains_compatible_reads_and_blocks_new_acceptance(client,settings,config):
    session,_,_=await login(client,config['users'][0]); headers=mutation_headers(settings,session)
    before=await client.get('/api/v1/preferences'); assert before.status_code==200
    try:
        rollout(config,False)
        status=await client.get('/api/v1/preferences/rollout'); assert status.status_code==200
        assert status.json()['new_writes_enabled'] is False
        result=await client.post('/api/v1/preferences/capture',headers=headers,json={'client_event_id':str(uuid4()),'text':'Always reply in Hindi'})
        assert result.status_code==503
        assert (await client.get('/api/v1/preferences')).status_code==200
        assert (await client.post('/api/v1/preferences/rollout',headers=headers,json={'enabled':True})).status_code==405
    finally: rollout(config,True)
    result=await client.post('/api/v1/preferences/capture',headers=headers,json={'client_event_id':str(uuid4()),'text':'Always reply in Hindi'})
    assert result.status_code==202

async def test_rollout_status_requires_authentication(client):
    assert (await client.get('/api/v1/preferences/rollout')).status_code==401

def test_changed_legacy_snapshot_invalidates_reviewed_manifest(config,customer,legacy,legacy_row):
    mappings,_,_=mapping(config,customer,legacy_row)
    with psycopg.connect(legacy.admin_dsn) as old,connection(config,customer,'migrator') as bank:
        plan=build_plan(old,bank,mappings)
        subject=uuid4()
        old.execute("INSERT INTO public.memories(subject_id,actor_id,content) VALUES(%s,%s,'synthetic new ordinary row')",(subject,subject))
        with pytest.raises(ValueError): apply_plan(old,bank,mappings,digest(plan))

def test_arbitrary_legacy_text_cannot_create_accepted_preference(config,customer,legacy,legacy_row):
    with connection(config,customer) as c: source=accept(c,payload={'text':'I prefer English'})[0]
    with connection(config,customer,'migrator') as c:
        c.execute('INSERT INTO banking.legacy_owner_links VALUES(%s,%s,%s)',
            (legacy_row['legacy_subject_id'],customer,'synthetic explicit mapping'))
    mappings=[dict(legacy_id=legacy_row['legacy_id'],customer_id=str(customer),source_id=str(source),value='en')]
    with psycopg.connect(legacy.admin_dsn) as old,connection(config,customer,'migrator') as bank:
        plan=build_plan(old,bank,mappings)
        assert plan['counts']['accepted_typed_source_mismatch']==1
        assert apply_plan(old,bank,mappings,digest(plan))['attempted_rows']==0
        assert bank.execute('SELECT count(*) FROM banking.preferences_current').fetchone()[0]==0

def test_rollback_allows_already_accepted_worker_to_finish_idempotently(config,customer):
    from jobs.banking_consumer import Consumer
    source,outbox=accepted(config,customer,'Always reply in Hindi')
    try:
        rollout(config,False)
        consumer=Consumer('',config['dsns']['worker'])
        assert consumer.run_preference(customer,outbox)==['applied']
        with connection(config,customer) as c:
            assert c.execute('SELECT value,last_sequence FROM banking.preferences_current').fetchone()==('hi',1)
            assert c.execute('SELECT count(*) FROM banking.accepted_user_events').fetchone()[0]==1
        assert consumer.run_preference(customer,outbox)==[]
    finally: rollout(config,True)

def test_disable_waits_for_accepted_write_transaction_to_commit(config,customer):
    accepted_event=Event(); release=Event(); started=Event(); process={}
    def accepting():
        with connection(config,customer) as c:
            source=accept(c,payload={'handler':'preferences.v1','text':'synthetic pending'})
            accepted_event.set(); assert release.wait(5)
            return source[0]
    def disabling():
        with psycopg.connect(config['dsns']['migrator']) as c:
            process['pid']=c.execute('SELECT pg_backend_pid()').fetchone()[0]
            generation=c.execute('SELECT generation FROM banking.memory_rollout').fetchone()[0]
            started.set()
            return c.execute('SELECT banking.set_memory_rollout(false,%s)',(generation,)).fetchone()[0]
    try:
        with ThreadPoolExecutor(2) as pool:
            accept_future=pool.submit(accepting); assert accepted_event.wait(3)
            disable_future=pool.submit(disabling); assert started.wait(3)
            waiting=False; deadline=time.monotonic()+3
            with psycopg.connect(config['admin_dsn'],autocommit=True) as c:
                while time.monotonic()<deadline:
                    row=c.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s',(process['pid'],)).fetchone()
                    if row and row[0]=='Lock': waiting=True; break
                    time.sleep(.01)
            assert waiting and not disable_future.done()
            release.set(); source=accept_future.result(3); assert disable_future.result(3)['new_writes_enabled'] is False
        with connection(config,customer) as c:
            assert c.execute('SELECT count(*) FROM banking.accepted_user_events WHERE id=%s',(source,)).fetchone()[0]==1
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState): accept(c,payload={'handler':'preferences.v1','text':'synthetic after disable'})
    finally:
        release.set(); rollout(config,True)

def test_legacy_guard_drains_real_inflight_writer_and_requires_fresh_review(tmp_path):
    fixture=create_legacy_fixture(protected_rows=1); row=fixture.records[0]; started=Event(); release=Event(); state={}
    with psycopg.connect(fixture.admin_dsn) as c: snapshot=inventory(c)['snapshot_sha256']
    def writer():
        with psycopg.connect(fixture.app_dsn) as c:
            subject=row['legacy_subject_id']; c.execute("SELECT set_config('app.subject_id',%s,true),set_config('app.actor_id',%s,true)",(subject,subject))
            c.execute("UPDATE public.memories SET content='synthetic in-flight old write' WHERE id=%s",(row['legacy_id'],))
            started.set(); assert release.wait(5)
    def installing():
        with psycopg.connect(fixture.admin_dsn) as c:
            state['pid']=c.execute('SELECT pg_backend_pid()').fetchone()[0]
            return install_guard(c,snapshot,tmp_path/'stale-review.json')
    with ThreadPoolExecutor(2) as pool:
        write=pool.submit(writer); assert started.wait(3)
        guard=pool.submit(installing); waiting=False; deadline=time.monotonic()+3
        with psycopg.connect(fixture.admin_dsn,autocommit=True) as c:
            while time.monotonic()<deadline:
                if state.get('pid'):
                    lock=c.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s',(state['pid'],)).fetchone()
                    if lock and lock[0]=='Lock': waiting=True; break
                time.sleep(.01)
        try: assert waiting and not guard.done()
        finally: release.set()
        write.result(3)
        with pytest.raises(ValueError): guard.result(3)
    with psycopg.connect(fixture.admin_dsn) as c:
        report=install_guard(c,inventory(c)['snapshot_sha256'],tmp_path/'fresh-review.json')
        assert report['existing_rows_unchanged']
    with psycopg.connect(fixture.app_dsn) as c:
        subject=row['legacy_subject_id']; c.execute("SELECT set_config('app.subject_id',%s,true),set_config('app.actor_id',%s,true)",(subject,subject))
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            c.execute("UPDATE public.memories SET content='synthetic stale resumption' WHERE id=%s",(row['legacy_id'],))
