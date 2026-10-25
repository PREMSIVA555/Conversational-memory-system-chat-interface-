"""T05.1 HTTP acceptance and persistence invariants in an isolated Docker world."""
import asyncio
import pytest
import pytest_asyncio
import psycopg
from pydantic import ValidationError
from scripts.p051_probe import Probe
from store.banking_support import Draft


@pytest_asyncio.fixture
async def probe():
    p = await Probe().start()
    try: yield p
    finally: await p.close()


async def draft(p, **fields):
    t = await p.step({'op': 'turn', 'save': 't'}); assert t['status'] == 202
    body = dict(source_turn_id='$t.turn_id', topic='general', summary='  Synthetic issue\r\nPlease check.  ', **fields)
    r = await p.step({'op': 'draft', 'body': body, 'save': 'p'}); assert r['status'] == 201, r
    return r['body']


def bound(p): return {'version': p['version'], 'payload_hash': p['payload_hash']}


@pytest.mark.asyncio
async def test_exact_preview_edit_cancel_and_confirmation_boundary(probe):
    p = await draft(probe)
    assert p['payload']['summary'] == 'Synthetic issue\nPlease check.'
    read = await probe.step({'op': 'get', 'id': p['id']}); assert read['body'] == p
    good = await probe.step({'op': 'confirm', 'id': p['id'], 'body': bound(p)})
    assert good['status'] == 503  # No case writer until T05.2, never a false success.
    edit = await probe.step({'op': 'edit', 'id': p['id'], 'body': {**bound(p), 'draft': {**p['payload'], 'summary': 'Revised issue'}}})
    assert edit['status'] == 201
    newer = edit['body']; assert newer['id'] != p['id'] and newer['version'] == 2 and newer['payload_hash'] != p['payload_hash']
    assert (await probe.step({'op': 'get', 'id': p['id']}))['body']['status'] == 'cancelled'
    assert (await probe.step({'op': 'confirm', 'id': p['id'], 'body': bound(p)}))['status'] == 409
    assert (await probe.step({'op': 'confirm', 'id': newer['id'], 'body': bound(p)}))['status'] == 409
    assert (await probe.step({'op': 'cancel', 'id': newer['id'], 'body': bound(newer)}))['status'] == 200
    assert (await probe.step({'op': 'confirm', 'id': newer['id'], 'body': bound(newer)}))['status'] == 409
    assert (await probe.step({'op': 'oracle'}))['bank_data_unchanged']


@pytest.mark.asyncio
@pytest.mark.parametrize('actor', ['A2', 'B'])
async def test_other_session_and_owner_denied(probe, actor):
    p = await draft(probe)
    for op in ['get', 'cancel', 'confirm']:
        assert (await probe.step({'op': op, 'actor': actor, 'id': p['id'], 'body': bound(p)}))['status'] == 404
    assert (await probe.step({'op': 'draft', 'actor': actor, 'body': p['payload']}))['status'] == 404
    assert (await probe.step({'op': 'latest', 'actor': actor, 'turn_id': p['source_turn_id']}))['status'] == 404


@pytest.mark.asyncio
async def test_expiry_revocation_and_erasure(probe):
    p = await draft(probe)
    await probe.step({'op': 'expire', 'id': p['id']})
    assert (await probe.step({'op': 'get', 'id': p['id']}))['body']['status'] == 'expired'
    for op in ['confirm', 'cancel']:
        assert (await probe.step({'op': op, 'id': p['id'], 'body': bound(p)}))['status'] == 409
    assert (await probe.step({'op': 'erase', 'turn_id': p['source_turn_id']}))['status'] == 200
    assert (await probe.step({'op': 'get', 'id': p['id']}))['status'] == 404
    async with probe.app.state.database.session(probe.contexts['A']) as c:
        assert not await (await c.execute('SELECT id FROM banking.support_proposals')).fetchall()
    await probe.step({'op': 'revoke'})
    assert (await probe.step({'op': 'get', 'id': p['id']}))['status'] == 401


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['account', 'card', 'loan'])
async def test_owned_references_and_foreign_resource_denial(probe, kind):
    await probe.step({'op': 'turn', 'save': 't'})
    body = dict(source_turn_id='$t.turn_id', topic=kind, resource_id='$'+kind+'_b', summary='Check this resource')
    foreign = await probe.step({'op': 'draft', 'body': body}); assert foreign['status'] == 404
    body['resource_id'] = '$'+kind+'_a'
    assert (await probe.step({'op': 'draft', 'body': body}))['status'] == 201


@pytest.mark.asyncio
async def test_concurrent_create_and_edit_atomicity(probe):
    await probe.step({'op': 'turn', 'save': 't'})
    body = dict(source_turn_id='$t.turn_id', topic='general', summary='Concurrent preview')
    created = await asyncio.gather(*(probe.step({'op': 'draft', 'body': body}) for _ in range(4)))
    assert sorted(r['status'] for r in created) == [201, 409, 409, 409]
    p = next(r['body'] for r in created if r['status'] == 201)
    edited = await asyncio.gather(*(probe.step({'op': 'edit', 'id': p['id'], 'body': {**bound(p), 'draft': {**p['payload'], 'summary': str(i)}}}) for i in range(4)))
    assert sorted(r['status'] for r in edited) == [201, 409, 409, 409]
    rows = (await probe.step({'op': 'oracle'}))['proposals']
    assert len(rows) == 2 and sum(row[3] == 'pending' for row in rows) == 1


@pytest.mark.asyncio
async def test_csrf_origin_extra_fields_and_database_immutability(probe):
    p = await draft(probe)
    for headers in [{'X-CSRF-Token': ''}, {'X-CSRF-Token': 'wrong'}, {'Origin': 'https://evil.invalid'}]:
        assert (await probe.step({'op': 'cancel', 'id': p['id'], 'body': bound(p), 'headers': headers}))['status'] == 403
    assert (await probe.step({'op': 'confirm', 'id': p['id'], 'body': {**bound(p), 'customer_id': probe.contexts['A'].customer_id}}))['status'] == 422
    with psycopg.connect(probe.world.settings.runtime_dsn) as c:
        assert c.execute('SELECT count(*) FROM banking.support_proposals').fetchone()[0] == 0
        c.execute("SELECT set_config('bank.customer_id',%s,true)", (probe.contexts['A'].customer_id,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with c.transaction(): c.execute("UPDATE banking.support_proposals SET payload='{}' WHERE id=%s", (p['id'],))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with c.transaction(): c.execute('DELETE FROM banking.support_proposals WHERE id=%s', (p['id'],))
    for tool in ['confirm', 'confirm_support', 'create_case', 'support.confirm']:
        assert (await probe.step({'op': 'model_plan', 'body': {'tool': tool, 'args': bound(p)}})) == {'accepted': False}


@pytest.mark.parametrize('change', [{'summary': '  '}, {'topic': 'payment'}, {'topic': 'card'}, {'resource_id': 'bad'}, {'customer_id': 'forged'}, {'summary': 'bad\x00text'}])
def test_draft_contract(change):
    with pytest.raises(ValidationError):
        Draft.model_validate(dict(source_turn_id='11111111-1111-1111-1111-111111111111', topic='general', summary='Valid', **{}) | change)
