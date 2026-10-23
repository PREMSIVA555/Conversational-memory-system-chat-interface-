import base64
from dataclasses import replace
from datetime import date
import hashlib
import hmac
import json
import time
from uuid import uuid4
import psycopg
import pytest
from pydantic import ValidationError
from store.banking_contracts import Money, ProviderError

def oracle(config,sql,args=()):
    with psycopg.connect(config['admin_dsn'],row_factory=psycopg.rows.dict_row) as c:
        return c.execute(sql,args).fetchall()

@pytest.mark.parametrize('value,currency,exp,text', [('0','JPY',0,'JPY 0'),('-1','JPY',0,'JPY -1'),('9007199254740993','INR',2,'INR 90071992547409.93'),('-9223372036854775808','KWD',3,'KWD -9223372036854775.808'),('9223372036854775807','USD',2,'USD 92233720368547758.07')])
def test_exact_money(value,currency,exp,text):
    money=Money(minor_units=value,currency=currency,exponent=exp)
    assert money.formatted()==text
    assert json.loads(money.model_dump_json())['minor_units']==value

@pytest.mark.parametrize('value,currency,exp',[(1,'INR',2),(1.2,'INR',2),(True,'INR',2),('01','INR',2),('-0','INR',2),('+1','INR',2),('1.0','INR',2),('9223372036854775808','INR',2),('-9223372036854775809','INR',2),('1','BTC',8),('1','inr',2),('1','INR',3),('1','INR',True)])
def test_money_rejects(value,currency,exp):
    with pytest.raises(ValidationError): Money(minor_units=value,currency=currency,exponent=exp)

@pytest.mark.asyncio
async def test_account_db_oracle_and_all_pages(service,world,config):
    p,(a,b),_,_=service; actual=[]; cursor=None
    while True:
        page=await p.list_accounts(a,limit=1,cursor=cursor)
        actual.extend(page.items)
        assert page.complete==(page.next_cursor is None)
        cursor=page.next_cursor
        if cursor is None: break
    expected=oracle(config,'SELECT * FROM banking.accounts WHERE customer_id=%s ORDER BY id',(world['owners'][0],))
    assert [r.id for r in actual]==[str(r['id']) for r in expected]
    for got,row in zip(actual,expected):
        assert got.current_balance.minor_units==str(row['current_minor'])
        assert got.available_balance.minor_units==str(row['available_minor'])
        assert got.source.as_of==row['as_of'] and got.source.version==str(row['version'])
        assert (await p.get_account(a,got.id))==got
    assert len((await p.list_accounts(b)).items)==1

@pytest.mark.asyncio
async def test_history_status_range_ties_all_pages(service,world,config):
    p,(a,b),_,_=service; got=[]; cursor=None; account=world['accounts'][0]
    while True:
        page=await p.list_transactions(a,account,start='2026-10-01',end='2026-10-19',limit=2,cursor=cursor)
        got.extend(page.items); cursor=page.next_cursor
        assert page.complete==(cursor is None)
        if cursor is None: break
    expected=oracle(config,'SELECT * FROM banking.account_transactions WHERE account_id=%s ORDER BY booked_on,id',(account,))
    assert len(got)==8 and [r.id for r in got]==[str(r['id']) for r in expected]
    for actual,row in zip(got,expected):
        assert actual.amount.minor_units==str(row['amount_minor'])
        assert actual.status==row['status'] and actual.booked_on==row['booked_on'] and actual.reason is None
        assert actual.source.as_of==row['as_of'] and actual.source.version==str(row['version'])
    selected=await p.list_transactions(a,account,start='2026-10-19',end='2026-10-19',status='posted')
    assert len(selected.items)==1 and selected.items[0].status=='posted'
    default=await p.list_transactions(a,account)
    assert (default.start,default.end)==(date(2026,9,20),date(2026,10,19))
    assert (await p.list_transactions(a,account,start='2025-10-19',end='2026-10-19')).complete

@pytest.mark.asyncio
@pytest.mark.parametrize('options',[{'limit':0},{'limit':101},{'limit':True},{'limit':1.0},{'start':'2026-10-20','end':'2026-10-19'},{'start':'2025-10-18','end':'2026-10-19'},{'start':'2026-1-01'},{'start':'2026-10-01T00:00:00Z'},{'status':'settled'}])
async def test_invalid_ranges(service,world,options):
    p,contexts,_,_=service
    with pytest.raises(ProviderError) as e: await p.list_transactions(contexts[0],world['accounts'][0],**options)
    assert e.value.status==422

@pytest.mark.asyncio
async def test_direct_foreign_unknown_ids_and_fake_context(service,world):
    p,(a,b),_,_=service
    for identity in [a,replace(a,customer_id=world['owners'][1])]:
        for fn in [p.get_account,p.list_transactions]:
            with pytest.raises(ProviderError) as e: await fn(identity,world['accounts'][3])
            assert e.value.status==(404 if identity==a else 401)
    for bad in [world['accounts'][0].upper(),world['accounts'][0].replace('-',''),'{'+world['accounts'][0]+'}','suffix',str(uuid4())]:
        with pytest.raises(ProviderError) as e: await p.get_account(a,bad)
        assert e.value.status in (422,404)
    with pytest.raises(ProviderError) as e: await p.list_accounts(replace(a,session_key=a.session_key+'fake'))
    assert e.value.status==401

@pytest.mark.asyncio
async def test_cursors_tamper_scope_query_expiry_revision(service,world,config):
    p,(a,b),_,_=service; account=world['accounts'][0]
    page=await p.list_transactions(a,account,limit=1)
    cursor=page.next_cursor
    assert (await p.list_transactions(a,account,limit=1,cursor=cursor)).items[0].id!=page.items[0].id
    for token,options,context,target in [(cursor+'x',{},a,account),('bad',{},a,account),('x'*5000,{},a,account),
        (cursor,{'status':'posted'},a,account),(cursor,{'limit':2},a,account),(cursor,{'end':'2026-10-18'},a,account),
        (cursor,{},a,world['accounts'][1]),(cursor,{},b,world['accounts'][3])]:
        with pytest.raises(ProviderError) as e: await p.list_transactions(context,target,**{'limit':1,**options},cursor=token)
        assert e.value.status==422
    body=json.loads(base64.urlsafe_b64decode(cursor.split('.')[0]+'==')); body['exp']=int(time.time())-1
    payload=base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip('=')
    expired=payload+'.'+base64.urlsafe_b64encode(hmac.digest(p._key,payload.encode(),'sha256')).decode().rstrip('=')
    with pytest.raises(ProviderError) as e: await p.list_transactions(a,account,limit=1,cursor=expired)
    assert e.value.code=='invalid_cursor'
    before=await p.get_account(a,account)
    listing=await p.list_accounts(a,limit=1)
    oracle(config,'UPDATE banking.accounts SET current_minor=42 WHERE id=%s RETURNING id',(account,))
    fresh=await p.get_account(a,account)
    assert fresh.current_balance.minor_units=='42' and fresh.source.version!=before.source.version
    with pytest.raises(ProviderError) as e: await p.list_transactions(a,account,limit=1,cursor=cursor)
    assert e.value.code=='stale_cursor'
    with pytest.raises(ProviderError) as e: await p.list_accounts(a,limit=1,cursor=listing.next_cursor)
    assert e.value.code=='stale_cursor'
    history=await p.list_transactions(a,account,limit=1)
    oracle(config,"UPDATE banking.account_transactions SET amount_minor=43,status='reversed' WHERE id=%s RETURNING id",(history.items[0].id,))
    fresh=await p.list_transactions(a,account,limit=1)
    assert fresh.items[0].amount.minor_units=='43' and fresh.items[0].status=='reversed'
    assert fresh.items[0].source.version!=history.items[0].source.version
    with pytest.raises(ProviderError) as e: await p.list_transactions(a,account,limit=1,cursor=history.next_cursor)
    assert e.value.code=='stale_cursor'

@pytest.mark.asyncio
async def test_session_revocation_and_expiry(service,world):
    p,(a,b),sessions,_=service
    await p.get_account(a,world['accounts'][0]); await sessions.revoke(a)
    with pytest.raises(ProviderError) as e: await p.get_account(a,world['accounts'][0])
    assert e.value.status==401
    assert (await p.list_accounts(b)).items
    token=await sessions.issue(world['owners'][0]); expired=await sessions.require(token)
    value=json.loads(await sessions.command('get',expired.session_key)); value['expires_at']=time.time()-1
    await sessions.command('set',expired.session_key,json.dumps(value),ex=60)
    with pytest.raises(ProviderError) as e: await p.list_accounts(expired)
    assert e.value.status==401

@pytest.mark.asyncio
async def test_real_sql_timeout_is_typed_and_recovers(service,world,config):
    p,(a,b),_,_=service
    with psycopg.connect(config['admin_dsn']) as blocker:
        blocker.execute('LOCK TABLE banking.accounts IN ACCESS EXCLUSIVE MODE')
        started=time.monotonic()
        with pytest.raises(ProviderError) as e: await p.get_account(a,world['accounts'][0])
        assert e.value.code=='unavailable' and e.value.retryable and time.monotonic()-started<3
    assert (await p.get_account(a,world['accounts'][0])).id==world['accounts'][0]

@pytest.mark.parametrize('role',['runtime','worker'])
def test_actual_role_rls_readonly_and_reset(config,world,role):
    with psycopg.connect(config['dsns'][role]) as c:
        assert c.execute('SELECT count(*) FROM banking.accounts').fetchone()[0]==0
        with c.transaction():
            c.execute("SELECT set_config('bank.customer_id',%s,true)",(world['owners'][0],))
            assert c.execute('SELECT count(*) FROM banking.accounts').fetchone()[0]==3
            assert c.execute('SELECT count(*) FROM banking.accounts WHERE id=%s',(world['accounts'][3],)).fetchone()[0]==0
            for statement,args in [('UPDATE banking.accounts SET current_minor=0 WHERE id=%s',(world['accounts'][0],)),
                ('DELETE FROM banking.accounts WHERE id=%s',(world['accounts'][0],)),
                ("INSERT INTO banking.accounts(customer_id,display_name,currency,current_minor,available_minor) VALUES(%s,'denied','INR',0,0)",(world['owners'][0],)),
                ('UPDATE banking.account_transactions SET amount_minor=0',()),('DELETE FROM banking.account_transactions',()),
                ("INSERT INTO banking.account_transactions(account_id,customer_id,amount_minor,status,booked_on) VALUES(%s,%s,0,'posted',current_date)",(world['accounts'][0],world['owners'][0]))]:
                with pytest.raises(psycopg.errors.InsufficientPrivilege),c.transaction(): c.execute(statement,args)
        c.commit()
        assert c.execute('SELECT count(*) FROM banking.accounts').fetchone()[0]==0

def test_db_composite_owner_currency_status_amount_constraints(config,world):
    with psycopg.connect(config['admin_dsn']) as c:
        for query,args,error in [
            ("INSERT INTO banking.account_transactions(account_id,customer_id,amount_minor,status,booked_on) VALUES(%s,%s,0,'posted',current_date)",(world['accounts'][0],world['owners'][1]),psycopg.errors.ForeignKeyViolation),
            ("UPDATE banking.accounts SET currency='BTC' WHERE id=%s",(world['accounts'][0],),psycopg.errors.CheckViolation),
            ("UPDATE banking.account_transactions SET status='settled' WHERE account_id=%s",(world['accounts'][0],),psycopg.errors.CheckViolation),
            ("UPDATE banking.accounts SET current_minor=9223372036854775808 WHERE id=%s",(world['accounts'][0],),psycopg.errors.NumericValueOutOfRange)]:
            with pytest.raises(error),c.transaction(): c.execute(query,args)
