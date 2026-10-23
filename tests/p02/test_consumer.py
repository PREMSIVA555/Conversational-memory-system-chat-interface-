from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4
import json
import os
import time
import psycopg
import pytest
from psycopg.types.json import Jsonb
from jobs.banking_consumer import Consumer
from store.banking_migrate import apply
from tests.p02.test_schema import connection, accept

@pytest.fixture(autouse=True)
def migration(config):
    with psycopg.connect(config['dsns']['migrator']) as c:
        apply(c,config['roles'],through='0006')

@pytest.fixture
def consumer(config):
    return Consumer(config['dsns']['dispatcher'],config['dsns']['worker'])

@pytest.fixture
def event(config,customer):
    with connection(config,customer) as c: source=accept(c)
    with psycopg.connect(config['admin_dsn']) as c:
        return c.execute('SELECT id FROM banking.outbox_events WHERE source_ref=%s',(source[0],)).fetchone()[0]

def claim(consumer,event):
    rows=consumer.claim(event_id=event)
    assert len(rows)==1
    return rows[0]

def expire(config,event):
    with psycopg.connect(config['admin_dsn']) as c:
        c.execute("UPDATE banking.outbox_events SET locked_until=clock_timestamp()-interval '1 second',available_at=clock_timestamp()-interval '1 second' WHERE id=%s",(event,))

def counts(config,event):
    with psycopg.connect(config['admin_dsn']) as c:
        return tuple(c.execute('SELECT count(*) FROM banking.'+table+' WHERE '+column+'=%s',(event,)).fetchone()[0]
                     for table,column in [('handler_receipts','event_id'),('local_operations','event_id'),('consumer_terminal_events','event_id')])

def observation(name,data):
    root=os.environ.get('P02_EVIDENCE_DIR')
    if root:
        p=Path(root);p.mkdir(parents=True,exist_ok=True)
        (p/(name+'.json')).write_text(json.dumps(data,indent=2,default=str),encoding='utf-8')

def test_crash_before_commit_rolls_back_receipt_and_effect(config,consumer,event):
    lease=claim(consumer,event)
    with pytest.raises(RuntimeError):
        with consumer._connection(lease) as c:
            assert c.execute('SELECT banking.complete_consumer(%s,%s,%s,%s)',
                (event,lease.owner,lease.generation,None)).fetchone()['complete_consumer']=='applied'
            raise RuntimeError('simulated crash before commit')
    assert counts(config,event)==(0,0,0)
    assert consumer.run_one(lease)=='applied'
    assert counts(config,event)==(1,1,1)
    observation('crash-before-commit',{'rolled_back_counts':[0,0,0],'replay_counts':counts(config,event)})

def test_invalid_effect_rolls_back_receipt(config,consumer,event):
    lease=claim(consumer,event)
    with pytest.raises(psycopg.errors.InvalidParameterValue): consumer.complete(lease,['invalid shape'])
    assert counts(config,event)==(0,0,0)
    assert consumer.run_one(lease)=='applied'

def test_commit_before_ack_replay_and_unknown_lookup(config,consumer,event,customer):
    lease=claim(consumer,event)
    assert consumer.run_one(lease)=='applied' # acknowledge deliberately discarded
    # New instance and fresh connection represent worker restart/reconciliation.
    restarted=Consumer(config['dsns']['dispatcher'],config['dsns']['worker'])
    result=restarted.lookup(customer,event)
    assert result['status']=='completed' and result['result']['handler']=='acceptance.record'
    assert restarted.complete(lease,{'ignored_duplicate':True})=='duplicate'
    assert restarted.run_one(lease)=='duplicate'
    assert counts(config,event)==(1,1,1)
    assert restarted.lookup(uuid4(),event) is None
    assert restarted.lookup(customer,uuid4()) is None # unknown, not failed/safe-new-request
    observation('lost-ack-restart',{'status':result['status'],'counts':counts(config,event),'unknown_is_not_recreated':True})

def test_concurrent_duplicate_effect_gate(config,consumer,event):
    lease=claim(consumer,event); barrier=Barrier(2)
    def run():
        barrier.wait(timeout=5)
        return consumer.complete(lease)
    with ThreadPoolExecutor(2) as pool:
        futures=[pool.submit(run) for _ in range(2)]
        results=[f.result(timeout=10) for f in futures]
    assert sorted(results)==['applied','duplicate'] and counts(config,event)==(1,1,1)
    observation('concurrent-duplicate',{'outcomes':results,'counts':counts(config,event)})

def test_paused_a_reclaimed_b_completed_a_cannot_publish(config,consumer,event):
    a=claim(consumer,event)
    consumer.checkpoint(a,{'stage':'a'})
    expire(config,event)
    b=claim(consumer,event)
    assert b.generation==a.generation+1 and b.owner!=a.owner
    consumer.checkpoint(b,{'stage':'b'})
    assert consumer.run_one(b)=='applied'
    for action in [lambda:consumer.checkpoint(a,{'stage':'stale'}),lambda:consumer.complete(a,{'stale':True}),lambda:consumer.heartbeat(a),lambda:consumer.fail(a,'permanent'),lambda:consumer.source(a)]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege): action()
    with psycopg.connect(config['admin_dsn']) as c:
        assert c.execute('SELECT generation,state FROM banking.consumer_checkpoints WHERE event_id=%s',(event,)).fetchone()==(b.generation,{'stage':'b'})
    assert counts(config,event)==(1,1,1)
    observation('a-paused-b-reclaimed',{'a_generation':a.generation,'b_generation':b.generation,'stale_actions_rejected':5,'counts':counts(config,event)})

@pytest.mark.parametrize('mutation',['owner','generation','customer','expired'])
def test_invalid_ownership_cannot_read_heartbeat_checkpoint_or_complete(config,consumer,event,mutation):
    lease=claim(consumer,event)
    bad={'owner':replace(lease,owner=uuid4()),'generation':replace(lease,generation=0),
         'customer':replace(lease,customer_id=uuid4()),'expired':lease}[mutation]
    if mutation=='expired': expire(config,event)
    for action in [lambda:consumer.source(bad),lambda:consumer.heartbeat(bad),lambda:consumer.checkpoint(bad,{}),lambda:consumer.complete(bad,{})]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege): action()
    assert counts(config,event)==(0,0,0)

def test_publication_reclaim_serialize_on_same_row(config,consumer,event):
    lease=claim(consumer,event)
    # Real open effect transaction holds rowlock; dispatcher SKIP LOCKED returns promptly.
    with consumer._connection(lease) as c:
        c.execute('SELECT banking.checkpoint_consumer(%s,%s,%s,%s)',(event,lease.owner,lease.generation,Jsonb({'held':True})))
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(consumer.claim,event_id=event).result(timeout=3)==[]
    assert consumer.run_one(lease)=='applied'

def test_expiry_checked_after_lock_wait(config,consumer,event):
    lease=claim(consumer,event)
    with psycopg.connect(config['admin_dsn']) as admin:
        admin.execute("UPDATE banking.outbox_events SET locked_until=clock_timestamp()+interval '0.2 seconds' WHERE id=%s",(event,))
    locked=Event()
    def holder():
        with psycopg.connect(config['admin_dsn']) as c:
            c.execute('SELECT id FROM banking.outbox_events WHERE id=%s FOR UPDATE',(event,));locked.set()
            time.sleep(.4)
    with ThreadPoolExecutor(1) as pool:
        future=pool.submit(holder); assert locked.wait(2)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):consumer.checkpoint(lease,{'late':True})
        future.result(timeout=3)
    assert counts(config,event)==(0,0,0)

def test_retry_budget_and_visible_dead_letter(config,consumer,event,customer):
    states=[]
    for attempt in range(3):
        lease=claim(consumer,event)
        assert lease.generation==attempt+1
        states.append(consumer.fail(lease,'transient_unavailable'))
        if attempt<2:
            assert consumer.claim(event_id=event)==[] # actual backoff
            with psycopg.connect(config['admin_dsn']) as c:
                c.execute("UPDATE banking.outbox_events SET available_at=clock_timestamp()-interval '1 second' WHERE id=%s",(event,))
    assert states==['pending','pending','dead_letter']
    assert consumer.claim(event_id=event)==[]
    assert consumer.lookup(customer,event)['failure_code']=='transient_unavailable'
    assert counts(config,event)==(0,0,1)
    observation('bounded-retries',{'states':states,'attempts':3,'counts':counts(config,event)})

def test_repeated_worker_crash_is_bounded(config,consumer,event,customer):
    for _ in range(3):
        claim(consumer,event);expire(config,event)
    assert consumer.claim(event_id=event)==[]
    assert consumer.lookup(customer,event)['failure_code']=='retry_exhausted'
    assert counts(config,event)==(0,0,1)

@pytest.mark.parametrize('field,value,code',[('schema_version',2,'unsupported_schema'),('event_type','future.accepted','unsupported_type')])
def test_unsupported_version_or_type_deadletters(config,consumer,event,customer,field,value,code):
    with psycopg.connect(config['admin_dsn']) as c:
        c.execute('UPDATE banking.outbox_events SET '+field+'=%s WHERE id=%s',(value,event))
    lease=claim(consumer,event)
    assert consumer.run_one(lease)=='dead_letter'
    assert consumer.lookup(customer,event)['failure_code']==code
    assert counts(config,event)==(0,0,1)

def test_permanent_and_unclassified_errors(config,consumer,event,customer):
    lease=claim(consumer,event)
    with pytest.raises(psycopg.errors.InvalidParameterValue):consumer.fail(lease,'raw private exception')
    assert consumer.fail(lease,'permanent')=='dead_letter'
    assert consumer.lookup(customer,event)['failure_code']=='permanent'

@pytest.mark.parametrize('limit,seconds',[(0,15),(33,15),(None,15),(1,0),(1,61),(1,None)])
def test_claim_bounds(config,consumer,event,limit,seconds):
    with pytest.raises(psycopg.errors.InvalidParameterValue): consumer.claim(limit=limit,lease_seconds=seconds,event_id=event)

def test_dispatcher_no_payload_or_mutation_and_no_context_fails_closed(config,consumer,event):
    lease=claim(consumer,event)
    for role in ['dispatcher','runtime']:
        with psycopg.connect(config['dsns'][role]) as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute('SELECT payload FROM banking.accepted_user_events') if role=='dispatcher' else c.execute('SELECT * FROM banking.claim_outbox(%s,1,15,%s)',(uuid4(),event))
    for statement,args in [('SELECT * FROM banking.outbox_events',()),('DELETE FROM banking.outbox_events',()),('SELECT banking.lock_consumer(%s,%s,%s)',(event,lease.owner,lease.generation))]:
        with psycopg.connect(config['dsns']['worker']) as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute(statement,args)
    with psycopg.connect(config['dsns']['worker']) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute('SELECT banking.complete_consumer(%s,%s,%s,%s)',(event,lease.owner,lease.generation,Jsonb({})))
    with psycopg.connect(config['admin_dsn']) as c:
        rows=c.execute("SELECT p.proname,p.prosecdef,p.proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='banking' AND p.proname IN ('claim_outbox','lock_consumer','complete_consumer')").fetchall()
        assert len(rows)==3 and all(definer and 'search_path=pg_catalog, banking' in settings for _,definer,settings in rows)

@pytest.mark.parametrize('mode,exit_code,expected',[('before-commit',17,(0,0,0)),('after-commit',18,(1,1,1))])
def test_real_worker_process_exit_around_commit(config,consumer,event,customer,mode,exit_code,expected):
    import subprocess
    import sys
    lease=claim(consumer,event)
    command=[sys.executable,'jobs/banking_consumer_probe.py',mode,str(event),str(customer),str(lease.owner),str(lease.generation)]
    run=subprocess.run(command,capture_output=True,text=True,timeout=10)
    assert run.returncode==exit_code,run.stdout
    # Backend disconnect rollback may complete just after OS process termination.
    deadline=time.monotonic()+3
    while counts(config,event)!=expected and time.monotonic()<deadline:time.sleep(.02)
    assert counts(config,event)==expected
    if mode=='before-commit':assert consumer.run_one(lease)=='applied'
    else:assert consumer.complete(lease)=='duplicate'
    assert counts(config,event)==(1,1,1)
    observation('process-exit-'+mode,{'child_exit_code':exit_code,'counts_after_exit':expected,'counts_after_same_event_replay':counts(config,event)})


def test_bounded_metadata_only_batch_claim(config,consumer,customer):
    expected=[]
    for _ in range(3):
        with connection(config,customer) as c: source=accept(c)
        with psycopg.connect(config['admin_dsn']) as c:
            row=c.execute("UPDATE banking.outbox_events SET available_at='2000-01-01' WHERE source_ref=%s RETURNING id",(source[0],)).fetchone()
            expected.append(row[0])
    batch=consumer.claim(limit=2)
    assert len(batch)==2 and {l.event_id for l in batch}.issubset(set(expected))
    assert all(set(l.__dict__)=={'event_id','customer_id','generation','owner'} for l in batch)
    for lease in batch: assert consumer.run_one(lease)=='applied'
    remaining=next(e for e in expected if e not in {l.event_id for l in batch})
    assert consumer.run_one(claim(consumer,remaining))=='applied'
    observation('bounded-batch',{'requested_limit':2,'returned':len(batch),'metadata_keys':sorted(batch[0].__dict__)})


def test_connectivity_unknown_does_not_mint_or_leak(consumer,monkeypatch):
    from jobs.banking_consumer import Lease
    calls=[]
    def unavailable(*args):
        calls.append('lookup')
        raise psycopg.OperationalError('private connection detail')
    monkeypatch.setattr(consumer,'lookup',unavailable)
    lease=Lease(uuid4(),uuid4(),1,uuid4())
    assert consumer.run_one(lease)=='unknown'
    assert calls==['lookup','lookup']

@pytest.mark.parametrize('publication',['checkpoint','complete','dead_letter'])
def test_expiry_during_publication_wait_rolls_back(config,consumer,event,publication):
    lease=claim(consumer,event)
    table='consumer_checkpoints' if publication=='checkpoint' else 'consumer_terminal_events'
    # Table lock is a deterministic delayed-publication fault, not a simulated clock.
    with psycopg.connect(config['admin_dsn']) as c:
        c.execute("UPDATE banking.outbox_events SET locked_until=clock_timestamp()+interval '0.25 seconds' WHERE id=%s",(event,))
    with ThreadPoolExecutor(1) as pool:
        with psycopg.connect(config['admin_dsn']) as blocker:
            blocker.execute('LOCK TABLE banking.'+table+' IN ACCESS EXCLUSIVE MODE')
            def publish():
                if publication=='checkpoint':return consumer.checkpoint(lease,{'late':True})
                if publication=='complete':return consumer.complete(lease)
                return consumer.fail(lease,'permanent')
            pending=pool.submit(publish)
            time.sleep(.45)
            assert not pending.done()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):pending.result(timeout=5)
    assert counts(config,event)==(0,0,0)
    with psycopg.connect(config['admin_dsn']) as c:
        assert c.execute('SELECT count(*) FROM banking.consumer_checkpoints WHERE event_id=%s',(event,)).fetchone()[0]==0
        assert c.execute('SELECT status FROM banking.outbox_events WHERE id=%s',(event,)).fetchone()[0]=='claimed'
    observation('post-wait-expiry-'+publication,{'receipt_effect_terminal_counts':counts(config,event),'publication_rolled_back':True})
