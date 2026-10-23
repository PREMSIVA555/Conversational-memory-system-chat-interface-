import base64
from datetime import datetime,timezone
import hashlib,hmac,json,time
from uuid import uuid4
import psycopg
import pytest
from pydantic import ValidationError
from store.banking_card_contracts import Card
from store.banking_contracts import ProviderError

pytestmark=pytest.mark.asyncio

async def test_same_suffix_is_two_owned_choices(service):
    p,ctx,db,w=service
    page=await p.list_cards(ctx[0],last_four='1111')
    assert {c.id for c in page.items}=={r['id'] for r in w.fixture['cards'][:2]}
    assert page.complete and page.next_cursor is None

async def test_credit_fields_are_exact_and_distinct(service):
    p,ctx,db,w=service;c=await p.get_card(ctx[0],w.fixture['cards'][1]['id'])
    assert [c.daily_limit.minor_units,c.credit_limit.minor_units,c.available_credit.minor_units,c.outstanding.minor_units]==['250000','9007199254740993','9007199254740000','993']
    assert c.credit_limit.formatted()=='INR 90071992547409.93' and c.source.version=='1'

async def test_debit_credit_fields_are_null_not_zero(service):
    p,ctx,db,w=service;c=await p.get_card(ctx[0],w.fixture['cards'][0]['id'])
    assert c.credit_limit is c.available_credit is c.outstanding is None
    assert c.daily_limit.formatted()=='INR 1500.00'

async def test_zero_and_pinned_zero_exponent(service):
    p,ctx,db,w=service;c=await p.get_card(ctx[0],w.fixture['cards'][3]['id'])
    assert c.status=='expired' and c.daily_limit.exponent==0 and c.daily_limit.formatted()=='JPY 0'

async def test_foreign_and_unknown_have_same_failure(service):
    p,ctx,db,w=service
    for id in [w.fixture['cards'][2]['id'],str(uuid4())]:
        with pytest.raises(ProviderError) as e:await p.get_card(ctx[0],id)
        assert (e.value.code,e.value.status)==('not_found',404)
    assert (await p.get_card(ctx[1],w.fixture['cards'][2]['id'])).daily_limit.currency=='USD'

async def test_actual_runtime_rls_readonly_and_forced_policies(service):
    p,ctx,db,w=service
    async with db.runtime.connection() as c:
        for table in ['cards','card_transactions']:
            assert (await (await c.execute('SELECT count(*) AS n FROM banking.'+table)).fetchone())['n']==0
            row=await (await c.execute("SELECT relrowsecurity,relforcerowsecurity,has_table_privilege(current_user,oid,'INSERT,UPDATE,DELETE') AS writes FROM pg_class WHERE oid=%s::regclass",('banking.'+table,))).fetchone()
            assert row['relrowsecurity'] and row['relforcerowsecurity'] and not row['writes']
    with psycopg.connect(w.settings.runtime_dsn) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute('UPDATE banking.cards SET daily_limit_minor=42')

async def test_owner_consistent_transaction_fk(service):
    p,ctx,db,w=service
    with psycopg.connect(w.admin_dsn) as c:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            c.execute("INSERT INTO banking.card_transactions(card_id,customer_id,amount_minor,status,occurred_at) VALUES(%s,%s,1,'posted',now())",(w.fixture['cards'][2]['id'],w.fixture['customers'][0]))

async def test_all_pages_with_timestamp_ties_and_utc_end_boundary(service):
    p,ctx,db,w=service;cursor=None;items=[];flags=[]
    while True:
        page=await p.list_card_transactions(ctx[0],w.fixture['cards'][1]['id'],start='2026-01-01',end='2026-01-15',limit=1,cursor=cursor)
        items+=page.items;flags.append(page.complete);cursor=page.next_cursor
        if page.complete:break
        assert len(flags)<10
    assert [i.id for i in items]==[r['id'] for r in w.fixture['card_transactions'][:4]]
    assert [i.amount.minor_units for i in items]==['-700','0','9007199254740993','-1']
    assert flags==[False,False,False,True] and all(i.reason is None for i in items)
    assert all(i.source.version=='1' and i.source.as_of==datetime(2026,1,15,12,tzinfo=timezone.utc) for i in items)

@pytest.mark.parametrize('status',['declined','pending','posted','reversed'])
async def test_status_filter_is_not_posted_spend(service,status):
    p,ctx,db,w=service;page=await p.list_card_transactions(ctx[0],w.fixture['cards'][1]['id'],start='2026-01-01',end='2026-01-15',status=status)
    assert len(page.items)==1 and page.items[0].status==status

@pytest.mark.parametrize('kwargs',[dict(limit=0),dict(limit=True),dict(last_four='11111'),dict(last_four='１１１１')])
async def test_invalid_list_input(service,kwargs):
    p,ctx,db,w=service
    with pytest.raises(ProviderError) as e:await p.list_cards(ctx[0],**kwargs)
    assert e.value.status==422

@pytest.mark.parametrize('kwargs',[dict(start='2026-02-01',end='2026-01-01'),dict(start='2026-1-1'),dict(status='paid'),dict(limit=101)])
async def test_invalid_history_input(service,kwargs):
    p,ctx,db,w=service
    with pytest.raises(ProviderError) as e:await p.list_card_transactions(ctx[0],w.fixture['cards'][1]['id'],**kwargs)
    assert e.value.status==422

@pytest.mark.parametrize('change',['range','status','limit','card','owner','tamper','operation'])
async def test_cursor_scope_binding(service,change):
    p,ctx,db,w=service;id=w.fixture['cards'][1]['id'];base=dict(start='2026-01-01',end='2026-01-15',limit=1)
    first=await p.list_card_transactions(ctx[0],id,**base);token=first.next_cursor;context=ctx[0]
    if change=='range':base['end']='2026-01-16'
    elif change=='status':base['status']='posted'
    elif change=='limit':base['limit']=2
    elif change=='card':id=w.fixture['cards'][0]['id']
    elif change=='owner':id=w.fixture['cards'][2]['id'];context=ctx[1]
    elif change=='tamper':token=('A' if token[0]!='A' else 'B')+token[1:]
    with pytest.raises(ProviderError) as e:
        if change=='operation':await p.list_cards(context,limit=1,cursor=token)
        else:await p.list_card_transactions(context,id,cursor=token,**base)
    assert e.value.code=='invalid_cursor'

async def test_cursor_stale_on_card_and_transaction_mutation(service):
    p,ctx,db,w=service;id=w.fixture['cards'][1]['id'];args=dict(start='2026-01-01',end='2026-01-15',limit=1)
    for mutation in ['card','transaction']:
        first=await p.list_card_transactions(ctx[0],id,**args)
        with psycopg.connect(w.admin_dsn) as c:
            if mutation=='card':c.execute('UPDATE banking.cards SET daily_limit_minor=1 WHERE id=%s',(id,))
            else:c.execute('UPDATE banking.card_transactions SET amount_minor=2 WHERE id=%s',(w.fixture['card_transactions'][0]['id'],))
        with pytest.raises(ProviderError) as e:await p.list_card_transactions(ctx[0],id,cursor=first.next_cursor,**args)
        assert (e.value.code,e.value.status)==('stale_cursor',409)

async def test_authentic_expired_cursor_without_freezing_security_clock(service):
    p,ctx,db,w=service;id=w.fixture['cards'][1]['id'];args=dict(start='2026-01-01',end='2026-01-15',limit=1)
    first=await p.list_card_transactions(ctx[0],id,**args);body=first.next_cursor.split('.')[0];data=json.loads(base64.urlsafe_b64decode(body+'='*(-len(body)%4)));data['exp']=int(time.time())-1
    body=base64.urlsafe_b64encode(json.dumps(data,sort_keys=True,separators=(',',':')).encode()).decode().rstrip('=');signature=base64.urlsafe_b64encode(hmac.digest(p._key,body.encode(),'sha256')).decode().rstrip('=')
    with pytest.raises(ProviderError) as e:await p.list_card_transactions(ctx[0],id,cursor=body+'.'+signature,**args)
    assert e.value.code=='invalid_cursor'

async def test_card_list_pages_are_distinct(service):
    p,ctx,db,w=service;page=await p.list_cards(ctx[0],limit=1);ids=[]
    while True:
        ids.extend(c.id for c in page.items)
        if page.complete:break
        page=await p.list_cards(ctx[0],limit=1,cursor=page.next_cursor)
    assert len(ids)==3 and len(set(ids))==3 and ids==sorted(ids)

async def test_invalid_source_shape_and_unregistered_secret_fields(service):
    p,ctx,db,w=service;c=await p.get_card(ctx[0],w.fixture['cards'][0]['id']);data=c.model_dump()
    for extras in [{'pan':'synthetic-forbidden'},{'credit_limit':data['daily_limit']},{'daily_limit':dict(minor_units='1',currency='JPY',exponent=2)}]:
        with pytest.raises(ValidationError):Card.model_validate({**data,**extras})

async def test_flag_disables_cards_and_preserves_accounts(service):
    p,ctx,db,w=service;p.cards_enabled=False
    with pytest.raises(ProviderError) as e:await p.get_card(ctx[0],w.fixture['cards'][0]['id'])
    assert e.value.code=='unsupported'
    assert (await p.get_account(ctx[0],w.fixture['accounts'][0]['id'])).id==w.fixture['accounts'][0]['id']

async def test_real_card_sql_lock_is_bounded(service):
    p,ctx,db,w=service
    with psycopg.connect(w.admin_dsn) as blocker:
        blocker.execute('LOCK TABLE banking.cards IN ACCESS EXCLUSIVE MODE');start=time.monotonic()
        with pytest.raises(ProviderError) as e:await p.get_card(ctx[0],w.fixture['cards'][0]['id'])
        assert e.value.code=='unavailable' and time.monotonic()-start<4
