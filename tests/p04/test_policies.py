"""Real PostgreSQL16/Redis7, immutable policy applicability and lane failures."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,json
from pathlib import Path
import psycopg
from psycopg import sql
import pytest
from evals.policy_world import PolicyWorld
from evals.banking_world import database_dsn
from store.banking_policies import stage,activate,retrieve


def document(version='v1',start='2026-01-01T00:00:00Z',end='2027-01-01T00:00:00Z',**changes):
    text='Synthetic credit card annual fee is INR 100. Fee waiver requires annual spend INR 10000.'
    d=dict(version=version,rule='annual-fee',product='credit-card',scope='retail',locale='en-IN',source=text,
        source_hash=hashlib.sha256(text.encode()).hexdigest(),effective_from=start,effective_until=end,expected_chunks=2,
        chunks=[dict(section='fee',text='Synthetic credit card annual fee is INR 100.',embedding=[1,0,0]),
                dict(section='waiver',text='Fee waiver requires annual spend INR 10000.',embedding=[0,1,0])])
    return d|changes


def manifest(*docs):
    return dict(synthetic=True,embedding_model='fixture-v1',dimensions=3,expected_documents=len(docs),documents=list(docs))


QUERY=dict(product='credit-card',scope='retail',locale='en-IN',question='annual fee',embedding=[1,0,0],embedding_model='fixture-v1',at='2026-06-01T00:00:00Z')


@pytest.fixture(scope='module')
def world():return PolicyWorld.create(json.loads(Path('.genesis/local/p01/config.json').read_text(encoding='utf-8')))


def connection(w,role='migrator'):
    return psycopg.connect(database_dsn(w.config['dsns'][role],w.name),autocommit=True)


def install(w,value):
    with connection(w) as c:
        old=c.execute('SELECT set_id FROM banking.policy_active').fetchone()
        sid=stage(c,value);activate(c,sid,expected=old[0] if old else None)
        return sid


def query(w,**changes):
    with connection(w,'runtime') as c:return retrieve(c,**(QUERY|changes))


def test_actual_docker_identity(world):
    identity=world.identity();assert identity['postgresql_version'].startswith('16.') and identity['redis_version'].startswith('7.')


def test_stage_is_not_activation_and_complete_snapshot(world):
    sid=install(world,manifest(document()))
    new=manifest(document('v2',start='2027-01-01T00:00:00Z',end='2028-01-01T00:00:00Z'))
    with connection(world) as c:
        second=stage(c,new)
        assert stage(c,new)==second
    result=query(world);assert result['set_id']==sid and {e['version'] for e in result['evidence']}=={'v1'}
    assert result['answer_support']=='not_evaluated'


@pytest.mark.parametrize('change',[dict(product='loan'),dict(scope='business'),dict(locale='hi-IN'),dict(at='2025-12-31T23:59:59.999999Z'),dict(at='2027-01-01T00:00:00Z')])
def test_ineligible_never_serves(world,change):
    install(world,manifest(document()));r=query(world,**change);assert r['status']=='missing' and r['evidence']==[]


def test_half_open_timezone_and_adjacent_versions(world):
    install(world,manifest(document('old',end='2026-06-01T00:00:00Z'),document('new',start='2026-06-01T05:30:00+05:30')))
    assert {e['version'] for e in query(world,at='2026-06-01T00:00:00Z')['evidence']}=={'new'}
    assert {e['version'] for e in query(world,at='2026-06-01T05:29:59.999999+05:30')['evidence']}=={'old'}


@pytest.mark.parametrize('fault',['partial','hash','overlap','dimension','nan','duplicate','source','timezone','synthetic'])
def test_bad_ingestion_preserves_active_and_count(world,fault):
    sid=install(world,manifest(document()));m=manifest(document('bad'))
    if fault=='partial':m['documents'][0]['chunks'].pop()
    if fault=='hash':m['documents'][0]['source_hash']='0'*64
    if fault=='overlap':m=manifest(document('one'),document('two'))
    if fault=='dimension':m['documents'][0]['chunks'][0]['embedding']=[1]
    if fault=='nan':m['documents'][0]['chunks'][0]['embedding']=[float('nan'),0,0]
    if fault=='duplicate':m['documents'][0]['chunks'][1]['section']='fee'
    if fault=='source':m['documents'][0]['chunks'][0]['text']='Invented rule'
    if fault=='timezone':m['documents'][0]['effective_from']='2026-01-01T00:00:00'
    if fault=='synthetic':m['synthetic']=False
    with connection(world) as c:
        before=c.execute('SELECT count(*) FROM banking.policy_sets').fetchone()
        with pytest.raises(ValueError):stage(c,m)
        assert c.execute('SELECT count(*) FROM banking.policy_sets').fetchone()==before
    assert query(world)['set_id']==sid


def test_stale_activation_and_unknown_set(world):
    sid=install(world,manifest(document()))
    with connection(world) as c:
        with pytest.raises(ValueError):activate(c,sid,expected='0'*64)
        with pytest.raises(ValueError):activate(c,'f'*64,expected=sid)
    assert query(world)['set_id']==sid


def test_runtime_cannot_write_and_migrator_cannot_mutate(world):
    install(world,manifest(document()))
    with connection(world,'runtime') as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):stage(c,manifest(document('no')))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute('DELETE FROM banking.policy_active')
    with connection(world) as c:
        with pytest.raises(psycopg.Error):c.execute('UPDATE banking.policy_sets SET manifest=manifest')
        with pytest.raises(psycopg.Error):c.execute('DELETE FROM banking.policy_sets')


def test_exact_lanes_rankings_and_degradation(world):
    install(world,manifest(document()))
    r=query(world);assert r['status']=='ok' and r['rankings']['vector'][0]=='["v1","fee"]'
    assert r['evidence'][0]['section']=='fee' and r['lanes']==dict(vector='ok',lexical='ok')
    r=query(world,embedding=None);assert r['status']=='degraded' and r['lanes']['vector']=='unavailable'
    with psycopg.connect(world.admin_dsn,autocommit=True) as c:
        role=sql.Identifier(world.config['roles']['runtime'])
        c.execute(sql.SQL('REVOKE EXECUTE ON FUNCTION banking.policy_lexical(text,text) FROM {}').format(role))
        try:
            r=query(world);assert r['status']=='degraded' and r['lanes']['lexical']=='unavailable' and r['evidence']
            r=query(world,embedding=None);assert r['status']=='unavailable' and r['evidence']==[]
        finally:c.execute(sql.SQL('GRANT EXECUTE ON FUNCTION banking.policy_lexical(text,text) TO {}').format(role))
    assert query(world)['status']=='ok'


def test_wrong_product_near_duplicate_and_current(world):
    install(world,manifest(document(),document('loan',product='loan'),document('hi',locale='hi-IN')))
    r=query(world);assert {e['version'] for e in r['evidence']}=={'v1'}
    assert query(world,at=None)['applicability']=='current'
    for change in [dict(at='2026-06-01'),dict(embedding_model='different'),dict(embedding=[0,0,0]),dict(limit=True)]:
        with pytest.raises(ValueError):query(world,**change)


def test_activation_rollback_and_snapshot(world):
    old=install(world,manifest(document()))
    with connection(world) as c:
        new=stage(c,manifest(document('next')))
        with pytest.raises(RuntimeError):
            with c.transaction():
                activate(c,new,expected=old)
                assert query(world)['set_id']==old
                raise RuntimeError('Simulated crash before commit')
    assert query(world)['set_id']==old
