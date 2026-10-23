"""T02.4 synthetic canaries against real constrained PostgreSQL/Redis roles."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
import json
import psycopg
from psycopg.types.json import Jsonb
import pytest
from store.banking_migrate import apply
from tests.p02.test_schema import connection
from tests.p02.test_preferences import accepted
from jobs.banking_consumer import Consumer
from tests.p01.conftest import settings, app, client, login, mutation_headers


@pytest.fixture(autouse=True)
def migrated(config):
    with psycopg.connect(config['dsns']['migrator']) as c:
        apply(c, config['roles'], through='0015')


@pytest.fixture
def conversation(config, customer):
    value = uuid4()
    with psycopg.connect(config['admin_dsn']) as c:
        c.execute('INSERT INTO banking.conversations(id,customer_id) VALUES(%s,%s)', (value, customer))
    return value


def capture(config, customer, conversation, text='synthetic canary Kestrel-713'):
    with connection(config, customer) as c:
        return c.execute('SELECT banking.create_memory(%s,%s,%s)', (uuid4(), conversation, text)).fetchone()[0]


def erase(config, customer, source):
    with connection(config, customer) as c:
        return c.execute('SELECT banking.erase_memory_source(%s)', (source,)).fetchone()[0]


def test_context_cas_is_session_and_conversation_bound(config, customer, conversation):
    with connection(config, customer) as c:
        value = c.execute('SELECT banking.write_context(%s,%s,0,%s)',
                          ('a'*64, conversation, Jsonb(['conversation:'+str(conversation)]))).fetchone()[0]
        assert value['version'] == 1
        assert c.execute('SELECT count(*) FROM banking.working_context WHERE session_binding=%s', ('b'*64,)).fetchone()[0] == 0
    with connection(config, customer) as c:
        with pytest.raises(psycopg.errors.SerializationFailure):
            c.execute('SELECT banking.write_context(%s,%s,0,%s)', ('a'*64, conversation, Jsonb([])))


def test_context_foreign_selection_rejected(config, customer, conversation):
    with connection(config, customer) as c:
        with pytest.raises(psycopg.errors.NoDataFound):
            c.execute('SELECT banking.write_context(%s,%s,0,%s)', ('a'*64, conversation, Jsonb(['conversation:'+str(uuid4())])))


@pytest.mark.parametrize('kind', ['memory','summary','vector','cache','checkpoint'])
def test_all_derivative_kinds_block_and_scrub(config, customer, conversation, kind):
    source = capture(config, customer, conversation)
    with connection(config, customer) as c:
        child = c.execute('SELECT banking.derive_memory(%s,%s,%s,%s)',
            ([source['source_id']], kind, conversation, 'Kestrel-713 child')).fetchone()[0]
        assert len(c.execute('SELECT * FROM banking.read_memories(%s,%s,NULL)', (conversation,'Kestrel-713')).fetchall()) == 2
    result = erase(config, customer, source['source_id'])
    assert result['active_block'] is True
    assert result['stores']['external_provider'] == 'not_configured'
    with connection(config, customer) as c:
        assert c.execute('SELECT * FROM banking.read_memories(%s,NULL,NULL)', (conversation,)).fetchall() == []
        assert c.execute('SELECT payload FROM banking.accepted_user_events WHERE id=%s', (source['source_id'],)).fetchone()[0] == {}
    with psycopg.connect(config['admin_dsn']) as c:
        rows = c.execute('SELECT content,deleted_at FROM banking.memory_derivatives WHERE id IN(%s,%s)', (source['id'],child['id'])).fetchall()
        assert len(rows) == 2 and all(text is None and deleted is not None for text,deleted in rows)
    assert erase(config, customer, source['source_id'])['status'] == 'deleted'


def test_multiparent_derivative_blocked_by_any_source(config, customer, conversation):
    first = capture(config, customer, conversation, 'synthetic A'); second = capture(config, customer, conversation, 'synthetic B')
    with connection(config, customer) as c:
        child = c.execute('SELECT banking.derive_memory(%s,%s,%s,%s)',
            ([first['source_id'],second['source_id']], 'summary',conversation,'combined synthetic AB')).fetchone()[0]
    erase(config, customer, first['source_id'])
    with connection(config, customer) as c:
        rows = c.execute('SELECT * FROM banking.read_memories(%s,NULL,NULL)', (conversation,)).fetchall()
        assert [str(row[0]) for row in rows] == [second['id']]


def test_backlog_and_stale_derivation_cannot_resurrect(config, customer, conversation):
    source = capture(config, customer, conversation)
    erase(config, customer, source['source_id'])
    with connection(config, customer) as c:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            c.execute('SELECT banking.derive_memory(%s,%s,%s,%s)', ([source['source_id']], 'summary',conversation,'stale'))
    with connection(config, customer, 'worker') as c:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            c.execute('SELECT banking.project_preference(%s,%s,%s,NULL,%s,%s)',
                (source['source_id'],'preferred_response_language','global','SET',Jsonb('hi')))


def test_typed_preference_erasure_cleans_value_proposal_span_and_fences_job(config, customer, conversation):
    event, outbox = accepted(config, customer, 'Always reply in Hindi', conversation)
    consumer = Consumer(config['dsns']['dispatcher'],config['dsns']['worker'])
    lease = consumer.claim(event_id=outbox)[0]
    erase(config, customer, event)
    assert consumer.run_one(lease) in ('fenced','lease_lost','dead_letter')
    with connection(config, customer) as c:
        assert c.execute('SELECT payload FROM banking.accepted_user_events WHERE id=%s',(event,)).fetchone()[0] == {}
        assert c.execute('SELECT proposals FROM banking.preference_proposals WHERE source_id=%s',(event,)).fetchone()[0] == []


def test_persisted_typed_preference_retains_only_ordering_tombstone(config, customer, conversation):
    event, outbox = accepted(config, customer, 'Always reply in Hindi', conversation)
    Consumer(config['dsns']['dispatcher'],config['dsns']['worker']).run_preference(customer,outbox)
    erase(config, customer, event)
    with connection(config, customer) as c:
        row = c.execute('SELECT value,tombstoned,last_sequence FROM banking.preferences_current').fetchone()
        assert row[0] is None and row[1] is True and row[2] > 0
        assert c.execute('SELECT normalized_value,source_span FROM banking.preference_events WHERE source_user_event_id=%s',(event,)).fetchone() == (None,None)


def test_restore_imports_backup_into_quarantine_and_scrubs_using_external_ledger(config, customer, conversation):
    source = capture(config, customer, conversation)
    erase(config, customer, source['source_id'])
    with psycopg.connect(config['admin_dsn']) as c:
        assert 'Kestrel-713' in json.dumps(c.execute('SELECT snapshot FROM banking.quarantined_memory_backups WHERE source_id=%s',(source['source_id'],)).fetchone()[0])
    with psycopg.connect(config['admin_dsn']) as c:
        c.execute("SELECT set_config('bank.customer_id',%s,true)",(str(customer),))
        result = c.execute('SELECT banking.restore_memory_check(%s)',(source['source_id'],)).fetchone()[0]
        assert result['restored_rows'] >= 2 and result['scrubbed_rows'] == result['restored_rows']
        assert result['remaining_content_rows'] == 0 and result['serving_enabled'] is False
        assert result['backup_status'] == 'retained_quarantined'
        assert c.execute('SELECT count(*) FROM pg_temp.t024_restore_quarantine WHERE content<>%s',(Jsonb({}),)).fetchone()[0] == 0


def test_raw_memory_and_backup_tables_inaccessible_to_runtime(config, customer):
    for table in ('memory_derivatives','memory_lineage','quarantined_memory_backups'):
        with connection(config, customer) as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute('SELECT * FROM banking.'+table)


def test_erasure_scrubs_live_consumer_checkpoint_and_invalidates_lease(config, customer, conversation):
    source = capture(config, customer, conversation)
    with psycopg.connect(config['admin_dsn']) as c:
        event = c.execute('SELECT id FROM banking.outbox_events WHERE source_ref=%s',(source['source_id'],)).fetchone()[0]
    consumer = Consumer(config['dsns']['dispatcher'],config['dsns']['worker'])
    lease = consumer.claim(event_id=event)[0]
    consumer.checkpoint(lease, dict(synthetic='checkpoint canary Wren-19'))
    erase(config, customer, source['source_id'])
    with psycopg.connect(config['admin_dsn']) as c:
        assert c.execute('SELECT count(*) FROM banking.consumer_checkpoints WHERE event_id=%s',(event,)).fetchone()[0] == 0
    assert consumer.run_one(lease) == 'dead_letter'


def test_forgetting_old_preference_preserves_newer_value(config, customer, conversation):
    old = accepted(config,customer,'Always reply in English',conversation)
    new = accepted(config,customer,'Always reply in Hindi',conversation)
    consumer = Consumer(config['dsns']['dispatcher'],config['dsns']['worker'])
    consumer.run_preference(customer,old[1]); consumer.run_preference(customer,new[1])
    erase(config,customer,old[0])
    with connection(config,customer) as c:
        assert c.execute('SELECT value,tombstoned FROM banking.preferences_current').fetchone() == ('hi',False)


def test_deleted_capture_client_event_cannot_be_reaccepted(config, customer, conversation):
    client_id=uuid4()
    with connection(config,customer) as c:
        source=c.execute('SELECT banking.create_memory(%s,%s,%s)',(client_id,conversation,'synthetic duplicate')).fetchone()[0]
    erase(config,customer,source['source_id'])
    with connection(config,customer) as c:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            c.execute('SELECT banking.create_memory(%s,%s,%s)',(client_id,conversation,'synthetic duplicate'))


def test_context_expiry_and_backup_retention_cleanup_are_real(config, customer, conversation):
    with connection(config,customer) as c:
        c.execute('SELECT banking.write_context(%s,%s,0,%s)',('a'*64,conversation,Jsonb([])))
    source=capture(config,customer,conversation)
    erase(config,customer,source['source_id'])
    with psycopg.connect(config['admin_dsn']) as c:
        c.execute("UPDATE banking.working_context SET expires_at=clock_timestamp()-interval '1 second' WHERE customer_id=%s",(customer,))
        c.execute("UPDATE banking.quarantined_memory_backups SET expires_at=clock_timestamp()-interval '1 second' WHERE source_id=%s",(source['source_id'],))
    with connection(config,customer) as c:
        result=c.execute('SELECT banking.memory_retention()').fetchone()[0]
        assert result['expired_backups_removed']==1
        assert c.execute('SELECT selections FROM banking.working_context').fetchone()[0]==[]
    with psycopg.connect(config['admin_dsn']) as c:
        assert c.execute('SELECT count(*) FROM banking.quarantined_memory_backups WHERE source_id=%s',(source['source_id'],)).fetchone()[0]==0


def test_cross_customer_source_cannot_derive_read_erase_or_restore(config,customer,conversation):
    source=capture(config,customer,conversation)
    outsider=uuid4(); outsiders_convo=uuid4()
    with psycopg.connect(config['admin_dsn']) as c:
        c.execute('INSERT INTO banking.customers(customer_id) VALUES(%s)',(outsider,))
        c.execute('INSERT INTO banking.conversations(id,customer_id) VALUES(%s,%s)',(outsiders_convo,outsider))
    for function in ('erase_memory_source','restore_memory_check'):
        with connection(config,outsider) as c:
            with pytest.raises(psycopg.errors.NoDataFound): c.execute('SELECT banking.'+function+'(%s)',(source['source_id'],))
    with connection(config,outsider) as c:
        assert c.execute('SELECT * FROM banking.read_memories(NULL,NULL,%s)',(source['id'],)).fetchall()==[]
    with connection(config,outsider) as c:
        with pytest.raises(psycopg.errors.NoDataFound):
            c.execute('SELECT banking.derive_memory(%s,%s,%s,%s)',([source['source_id']],'summary',outsiders_convo,'synthetic'))


def test_concurrent_derivation_and_forgetting_cannot_leave_serving_content(config,customer,conversation):
    source=capture(config,customer,conversation)
    barrier=Barrier(2)
    def derive():
        try:
            with connection(config,customer) as c:
                barrier.wait(timeout=5)
                c.execute('SELECT banking.derive_memory(%s,%s,%s,%s)',([source['source_id']],'cache',conversation,'concurrent canary'))
            return 'committed_before_erasure'
        except psycopg.errors.ObjectNotInPrerequisiteState:
            return 'blocked_after_erasure'
    def forget():
        barrier.wait(timeout=5)
        return erase(config,customer,source['source_id'])
    with ThreadPoolExecutor(2) as pool:
        one=pool.submit(derive); two=pool.submit(forget)
        assert one.result(timeout=10) in ('committed_before_erasure','blocked_after_erasure')
        assert two.result(timeout=10)['active_block'] is True
    with connection(config,customer) as c:
        assert c.execute('SELECT * FROM banking.read_memories(%s,NULL,NULL)',(conversation,)).fetchall()==[]


@pytest.mark.asyncio
async def test_http_context_does_not_transfer_to_new_session_or_after_logout(app,settings,config):
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=settings.origin) as first:
        a,_,_=await login(first,config['users'][0]); ah=mutation_headers(settings,a)
        conversation=(await first.post('/api/v1/conversations',json={},headers=ah)).json()['id']
        assert (await first.put('/api/v1/context/'+conversation,json=dict(expected_version=0,selections=['conversation:'+conversation]),headers=ah)).status_code==200
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=settings.origin) as second:
            await login(second,config['users'][0])
            assert (await second.get('/api/v1/context/'+conversation)).json()['selections']==[]
        assert (await first.post('/api/v1/auth/logout',json={},headers=ah)).status_code==200
        assert (await first.get('/api/v1/context/'+conversation)).status_code==401


@pytest.mark.asyncio
async def test_http_context_expiry_retention_and_invalid_selection(client,settings,config):
    a,_,_=await login(client,config['users'][0]); headers=mutation_headers(settings,a)
    conversation=(await client.post('/api/v1/conversations',json={},headers=headers)).json()['id']
    response=await client.put('/api/v1/context/'+conversation,json=dict(expected_version=0,selections=['conversation:not-a-uuid']),headers=headers)
    assert response.status_code==422
    assert (await client.put('/api/v1/context/'+conversation,json=dict(expected_version=0,selections=[]),headers=headers)).status_code==200
    with psycopg.connect(config['admin_dsn']) as c:
        c.execute("UPDATE banking.working_context SET expires_at=clock_timestamp()-interval '1 second' WHERE conversation_id=%s",(conversation,))
    assert (await client.get('/api/v1/context/'+conversation)).json()['version']==0
    policy=(await client.get('/api/v1/memory/retention')).json()
    assert policy['working_context_ttl_seconds']==1800 and policy['quarantined_backup_ttl_seconds']==86400
    renewed=await client.put('/api/v1/context/'+conversation,json=dict(expected_version=0,selections=[]),headers=headers)
    assert renewed.status_code==200 and renewed.json()['version']==2
    stale=await client.put('/api/v1/context/'+conversation,json=dict(expected_version=1,selections=['conversation:'+conversation]),headers=headers)
    assert stale.status_code==409


@pytest.mark.asyncio
async def test_http_context_history_erasure_restore_and_csrf(client, settings, config):
    session,_,_ = await login(client,config['users'][0]); headers = mutation_headers(settings,session)
    conversation = (await client.post('/api/v1/conversations',json={},headers=headers)).json()['id']
    assert (await client.get('/api/v1/context/'+conversation)).json()['version'] == 0
    response = await client.put('/api/v1/context/'+conversation,json=dict(expected_version=0,selections=['conversation:'+conversation]),headers=headers)
    assert response.status_code == 200 and response.json()['version'] == 1
    response = await client.put('/api/v1/context/'+conversation,json=dict(expected_version=0,selections=[]),headers=headers)
    assert response.status_code == 409
    source = (await client.post('/api/v1/memory',json=dict(client_event_id=str(uuid4()),conversation_id=conversation,text='synthetic API canary Tern-17'),headers=headers)).json()
    result = (await client.get('/api/v1/memory',params=dict(conversation_id=conversation,query='Tern-17'))).json()
    assert result['lane_status']=='lexical_history' and result['semantic_status'].startswith('disabled')
    assert result['items'][0]['authority']=='historical_user_memory_not_current_bank_facts'
    assert (await client.delete('/api/v1/memory/sources/'+source['source_id'])).status_code == 403
    assert (await client.delete('/api/v1/memory/sources/'+source['source_id'],headers=headers)).json()['active_block'] is True
    assert (await client.get('/api/v1/memory/'+source['id'])).status_code == 404
    response = await client.post('/api/v1/memory/restore-check',json=dict(source_id=source['source_id']),headers=headers)
    assert response.json()['remaining_content_rows'] == 0
