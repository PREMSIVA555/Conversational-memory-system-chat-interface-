from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
import json
import os
from pathlib import Path
import psycopg
from psycopg.types.json import Jsonb
import pytest
from store.banking_migrate import apply
from store.banking_legacy_plan import classify

KEY='preferred_response_language'

def connection(config,customer,role='runtime'):
    c=psycopg.connect(config['dsns'][role])
    c.execute("SELECT set_config('bank.customer_id',%s,true)",(str(customer),))
    return c

def accept(c,client=None,kind='chat',payload=None,conversation=None):
    return c.execute('SELECT * FROM banking.accept_user_event(%s,%s,%s,%s)',
      (client or uuid4(),kind,conversation,Jsonb(payload or {'text':'synthetic preference source'}))).fetchone()

def project(c,event,value='en',operation='SET',scope='global',conversation=None):
    return c.execute('SELECT banking.project_preference(%s,%s,%s,%s,%s,%s)',
        (event[0],KEY,scope,conversation,operation,Jsonb(value) if value is not None else None)).fetchone()[0]

def test_first_insert_race(config,customer):
    with connection(config,customer) as c:
        old=accept(c); new=accept(c)
    barrier=Barrier(2)
    def write(event,value):
        with connection(config,customer,'worker') as c:
            barrier.wait(timeout=5)
            return project(c,event,value)
    with ThreadPoolExecutor(2) as pool:
        a=pool.submit(write,old,'en'); b=pool.submit(write,new,'hi')
        results=[a.result(timeout=10),b.result(timeout=10)]
    with connection(config,customer) as c:
        row=c.execute('SELECT value,last_sequence,count(*) OVER() FROM banking.preferences_current').fetchone()
        assert row==('hi',new[3],1)
    assert 'persisted' in results

def test_delete_blocks_late_set_and_duplicate(config,customer):
    with connection(config,customer) as c:
        old=accept(c); new=accept(c)
    with connection(config,customer,'worker') as c:
        assert project(c,new,None,'DELETE')=='persisted'
        assert project(c,old,'en')=='ignored_stale'
        assert project(c,new,None,'DELETE')=='persisted'
        assert c.execute('SELECT value,tombstoned,version,last_sequence FROM banking.preferences_current').fetchone()==(None,True,1,new[3])

def test_acceptance_idempotency_and_atomic_rollback(config,customer):
    client=uuid4()
    with connection(config,customer) as c:
        first=accept(c,client); assert accept(c,client)==first
    with connection(config,customer) as c:
        with pytest.raises(psycopg.errors.UniqueViolation):
            accept(c,client,payload={'text':'changed'})
    with pytest.raises(RuntimeError):
        with connection(config,customer) as c:
            accept(c); raise RuntimeError('intentional rollback')
    with psycopg.connect(config['admin_dsn']) as c:
        assert c.execute('SELECT count(*) FROM banking.accepted_user_events WHERE customer_id=%s',(customer,)).fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM banking.outbox_events WHERE customer_id=%s',(customer,)).fetchone()[0]==1
        assert c.execute('SELECT acceptance_sequence FROM banking.customers WHERE customer_id=%s',(customer,)).fetchone()[0]==1

def test_panel_cas_precedes_acceptance(config,customer):
    payload={'key':KEY,'scope_key':'global','operation':'SET','value':'hi','expected_version':0}
    client=uuid4()
    with connection(config,customer) as c:
        source=accept(c,client,'panel',payload); assert accept(c,client,'panel',payload)==source
        assert c.execute('SELECT value,version FROM banking.preferences_current').fetchone()==('hi',1)
    with connection(config,customer) as c:
        with pytest.raises(psycopg.errors.SerializationFailure): accept(c,kind='panel',payload=payload)
    with psycopg.connect(config['admin_dsn']) as c:
        assert c.execute('SELECT acceptance_sequence FROM banking.customers WHERE customer_id=%s',(customer,)).fetchone()[0]==1

@pytest.mark.parametrize('value,scope,conversation', [('fr','global',None),(None,'global',None),('en','conversation:invalid',None)])
def test_typed_and_scope_constraints(config,customer,value,scope,conversation):
    with connection(config,customer) as c: source=accept(c)
    with connection(config,customer,'worker') as c:
        with pytest.raises(psycopg.errors.CheckViolation): project(c,source,value,scope=scope,conversation=conversation)

def test_context_and_raw_mutation_denied(config,customer):
    for role in ['runtime','worker']:
        with psycopg.connect(config['dsns'][role]) as c:
            assert c.execute('SELECT count(*) FROM banking.accepted_user_events').fetchone()[0]==0
            with pytest.raises(psycopg.errors.InsufficientPrivilege): c.execute('DELETE FROM banking.preferences_current')
    with psycopg.connect(config['dsns']['runtime']) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege): accept(c)
    with connection(config,customer,'worker') as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege): accept(c)

def test_cross_owner_conversation_rejected(config,customer):
    other=uuid4(); conversation=uuid4()
    with psycopg.connect(config['admin_dsn']) as c:
        c.execute('INSERT INTO banking.customers(customer_id) VALUES(%s)',(other,))
        c.execute('INSERT INTO banking.conversations(id,customer_id) VALUES(%s,%s)',(conversation,other))
    with connection(config,customer) as c:
        with pytest.raises(psycopg.errors.ForeignKeyViolation): accept(c,conversation=conversation)

def test_additive_reapply_and_schema_fields(config,customer):
    with connection(config,customer) as c: source=accept(c)
    with psycopg.connect(config['dsns']['migrator']) as c: apply(c,config['roles'],through='0003')
    with connection(config,customer) as c:
        assert c.execute('SELECT id FROM banking.accepted_user_events').fetchone()[0]==source[0]
    with psycopg.connect(config['admin_dsn']) as c:
        assert c.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema='banking' AND table_name='outbox_events' AND column_name IN ('lease_generation','fencing_token','locked_until','locked_by')").fetchone()[0]==4
        assert c.execute("SELECT count(*) FROM pg_class WHERE relnamespace='banking'::regnamespace AND relname IN ('accepted_user_events','preference_events','preferences_current','outbox_events','handler_receipts','memory_lineage') AND relrowsecurity AND relforcerowsecurity").fetchone()[0]==6

def test_legacy_classification_counts_and_no_rewrite():
    rows=[{'key':'language','value':'Java'}, {'customer_id':'synthetic','key':KEY,'value':'hi'},
      {'customer_id':'synthetic','source_user_event_id':'source','sequence':1,'key':KEY,'value':'C++','scope_key':'global'},
      {'customer_id':'synthetic','source_user_event_id':'source','sequence':2,'key':KEY,'value':'hi','scope_key':'global'}]
    before=json.dumps(rows,sort_keys=True); result=classify(rows)
    assert json.dumps(rows,sort_keys=True)==before
    assert result['input_rows']==result['classified_rows']==4 and result['writes']==0
    assert sum(result['counts'].values())==4 and len(result['counts'])==4
    assert 'Java' not in json.dumps(result) and 'C++' not in json.dumps(result)
    destination=os.environ.get('P02_EVIDENCE_DIR')
    if destination:
        Path(destination).mkdir(parents=True,exist_ok=True)
        (Path(destination)/'legacy-dry-run.json').write_text(json.dumps(result,indent=2),encoding='utf-8')

def test_conversation_scope_must_match_accepted_source(config,customer):
    a,b=uuid4(),uuid4()
    with connection(config,customer) as c:
        for conversation in [a,b]:
            c.execute('INSERT INTO banking.conversations(id,customer_id) VALUES(%s,%s)',(conversation,customer))
        source=accept(c,conversation=a)
    with connection(config,customer,'worker') as c:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            project(c,source,scope='conversation:'+str(b),conversation=b)

def test_lineage_derivative_owner_and_receipt_uniqueness(config,customer):
    other=uuid4(); derivative=uuid4()
    with connection(config,customer) as c: source=accept(c)
    with psycopg.connect(config['admin_dsn']) as c:
        c.execute('INSERT INTO banking.customers(customer_id) VALUES(%s)',(other,))
        c.execute("INSERT INTO banking.memory_derivatives(id,customer_id,kind) VALUES(%s,%s,'summary')",(derivative,other))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with c.transaction():
                c.execute("INSERT INTO banking.memory_lineage(derivative_id,customer_id,source_user_event_id,derivative_kind) VALUES(%s,%s,%s,'summary')",(derivative,customer,source[0]))
        event=c.execute('SELECT id FROM banking.outbox_events WHERE source_ref=%s',(source[0],)).fetchone()[0]
        statement="INSERT INTO banking.handler_receipts(event_id,customer_id,handler_name,handler_version,outcome) VALUES(%s,%s,'test','1','synthetic')"
        c.execute(statement,(event,customer))
        with pytest.raises(psycopg.errors.UniqueViolation):
            with c.transaction(): c.execute(statement,(event,customer))

def test_recorded_migration_checksum_cannot_change(config,monkeypatch):
    from types import SimpleNamespace
    import store.banking_migrate as migration
    # Replace only the checksum calculator in this module, never the source or ledger.
    monkeypatch.setattr(migration,'hashlib',SimpleNamespace(sha256=lambda _:SimpleNamespace(hexdigest=lambda:'0'*64)))
    with psycopg.connect(config['dsns']['migrator']) as c:
        with pytest.raises(ValueError,match='checksum changed'):
            migration.apply(c,config['roles'],through='0003')
