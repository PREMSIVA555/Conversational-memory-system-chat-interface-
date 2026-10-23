"""Actual PostgreSQL16/Redis7 provider checks against guarded LoanWorld only."""
import asyncio
from datetime import datetime,timezone
import json
from pathlib import Path
import time
import psycopg
import pytest
import pytest_asyncio
from pydantic import ValidationError
from auth.sessions import Sessions
from store.banking import Database
from store.banking_provider import AccountProvider
from store.banking_contracts import ProviderError
from store.banking_loan_contracts import Loan
from evals.loan_world import LoanWorld

@pytest.fixture(scope='module')
def loan_world():return LoanWorld.create(json.loads(Path('.genesis/local/p01/config.json').read_text()))

@pytest_asyncio.fixture
async def loans(loan_world):
    loan_world.reset();sessions=Sessions(loan_world.settings);db=Database(loan_world.settings,sessions);await db.open()
    contexts=[await sessions.require(await sessions.issue(owner)) for owner in loan_world.fixture['customers']]
    provider=AccountProvider(db,b't042-synthetic-cursor-key-32bytes!',clock=lambda:datetime(2026,1,15,tzinfo=timezone.utc),cards_enabled=True,loans_enabled=True)
    try:yield provider,contexts,db,loan_world
    finally:
        for context in contexts:await sessions.revoke(context)
        await db.close();await sessions.close()

def ids(world,key):return [r['id'] for r in world.fixture[key]]

@pytest.mark.asyncio
async def test_exact_loan_dto_and_three_exponents(loans):
    p,(a,b),_,w=loans;ls=ids(w,'loans');page=await p.list_loans(a)
    assert {v.id for v in page.items}=={ls[0],ls[1],ls[3]}
    large=await p.get_loan(a,ls[0]);assert large.principal.minor_units=='9007199254740993' and large.apr_percent=='12.34567890'
    assert large.principal.formatted()=='INR 90071992547409.93' and large.monthly_payment.formatted()=='INR 100.00'
    assert (await p.get_loan(a,ls[1])).principal.formatted()=='JPY 0'
    kwd=await p.get_loan(a,ls[3]);assert kwd.principal.formatted()=='KWD 12.345' and kwd.apr_percent=='0.00000001'
    assert (await p.get_loan(b,ls[2])).principal.currency=='USD'

@pytest.mark.asyncio
@pytest.mark.parametrize('method',['get_loan','list_emi_schedule','list_loan_payments'])
async def test_foreign_unknown_and_positive_owner(loans,method):
    p,(a,b),_,w=loans;ls=ids(w,'loans')
    await getattr(p,method)(b,ls[2])
    for id in [ls[2],'99999999-9999-4999-8999-999999999999']:
        with pytest.raises(ProviderError) as e:await getattr(p,method)(a,id)
        assert e.value.code=='not_found' and e.value.status==404

@pytest.mark.asyncio
async def test_separate_schedule_missing_and_partial_payments_all_pages(loans):
    p,(a,_),_,w=loans;l=ids(w,'loans')[0];ss=ids(w,'emi_schedule');ps=ids(w,'loan_payments')
    for method,key,expected in [('list_emi_schedule','schedule',ss[:4]),('list_loan_payments','payments',ps[:4])]:
        found=[];cursor=None
        while True:
            page=await getattr(p,method)(a,l,start='2026-01-01',end='2026-01-31',limit=1,cursor=cursor)
            found.extend(x.id for x in page.items)
            if page.complete:assert page.next_cursor is None;break
            assert page.next_cursor;cursor=page.next_cursor
        assert found==expected
    empty=await p.list_loan_payments(a,l,installment_id=ss[1],start='2026-01-01',end='2026-01-31')
    assert empty.items==[] and empty.complete
    partial=await p.list_loan_payments(a,l,installment_id=ss[0],start='2026-01-01',end='2026-01-31')
    assert [(x.amount.minor_units,x.status) for x in partial.items]==[('4000','posted'),('6000','pending')]
    assert all(x.reason is None for x in partial.items)
    sched=await p.list_emi_schedule(a,l,start='2026-01-01',end='2026-01-31')
    assert [x.due_on.isoformat() for x in sched.items]==['2026-01-10','2026-01-15','2026-01-15','2026-01-31']
    assert sched.items[2].scheduled_amount.minor_units=='0' and not hasattr(sched.items[1],'paid')

@pytest.mark.asyncio
@pytest.mark.parametrize('status',['posted','pending','declined','reversed'])
async def test_actual_payment_status_filter(loans,status):
    p,(a,_),_,w=loans;r=await p.list_loan_payments(a,ids(w,'loans')[0],start='2026-01-01',end='2026-01-31',status=status)
    assert len(r.items)==1 and r.items[0].status==status

@pytest.mark.asyncio
@pytest.mark.parametrize('variant',['range','status','size','owner','loan','installment','operation','tamper','expired'])
async def test_cursor_bindings(loans,variant):
    p,(a,b),_,w=loans;ls=ids(w,'loans');args=dict(loan_id=ls[0],start='2026-01-01',end='2026-01-31',limit=1)
    page=await p.list_loan_payments(a,**args);args['cursor']=page.next_cursor;context=a;method=p.list_loan_payments
    if variant=='range':args['end']='2026-01-30'
    if variant=='status':args['status']='posted'
    if variant=='size':args['limit']=2
    if variant=='owner':context=b;args['loan_id']=ls[2]
    if variant=='loan':args['loan_id']=ls[1]
    if variant=='installment':args['installment_id']=ids(w,'emi_schedule')[0]
    if variant=='operation':method=p.list_emi_schedule
    if variant=='tamper':args['cursor']='x'+args['cursor']
    if variant=='expired':
        import base64,hmac
        body=args['cursor'].split('.')[0];value=json.loads(base64.urlsafe_b64decode(body+'='*(-len(body)%4)));value['exp']=int(time.time())-1
        body=base64.urlsafe_b64encode(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).decode().rstrip('=');args['cursor']=body+'.'+base64.urlsafe_b64encode(hmac.digest(p._key,body.encode(),'sha256')).decode().rstrip('=')
    with pytest.raises(ProviderError) as e:await method(context,**args)
    assert e.value.code=='invalid_cursor'

@pytest.mark.asyncio
@pytest.mark.parametrize('table',['loans','emi_schedule','loan_payments'])
async def test_source_updates_invalidate_both_page_types(loans,table):
    p,(a,_),_,w=loans;l=ids(w,'loans')[0];args=dict(loan_id=l,start='2026-01-01',end='2026-01-31',limit=1)
    old=[await p.list_emi_schedule(a,**args),await p.list_loan_payments(a,**args)]
    with psycopg.connect(w.admin_dsn) as c:
        if table=='loans':c.execute('UPDATE banking.loans SET monthly_payment_minor=monthly_payment_minor+1 WHERE id=%s',(l,))
        elif table=='emi_schedule':c.execute('UPDATE banking.emi_schedule SET scheduled_minor=scheduled_minor+1 WHERE id=%s',(ids(w,'emi_schedule')[0],))
        else:c.execute('UPDATE banking.loan_payments SET amount_minor=amount_minor+1 WHERE id=%s',(ids(w,'loan_payments')[0],))
    for method,page in zip([p.list_emi_schedule,p.list_loan_payments],old):
        with pytest.raises(ProviderError) as e:await method(a,**args,cursor=page.next_cursor)
        assert e.value.code=='stale_cursor'

@pytest.mark.asyncio
async def test_real_roles_rls_owner_fks_and_no_writes(loans):
    p,(a,_),db,w=loans
    with psycopg.connect(w.settings.runtime_dsn) as c:
        assert c.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()==(False,False)
        for table in ['loans','emi_schedule','loan_payments']:
            assert c.execute('SELECT count(*) FROM banking.'+table).fetchone()[0]==0
            assert c.execute('SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=%s::regclass',('banking.'+table,)).fetchone()==(True,True)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute('UPDATE banking.loans SET principal_minor=principal_minor')
    with psycopg.connect(w.admin_dsn) as c:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with c.transaction():c.execute("INSERT INTO banking.emi_schedule(loan_id,customer_id,installment_number,due_on,scheduled_minor) VALUES(%s,%s,99,'2026-01-01',1)",(ids(w,'loans')[0],w.fixture['customers'][1]))

@pytest.mark.asyncio
async def test_flag_and_real_sql_timeout(loans):
    p,(a,_),_,w=loans;l=ids(w,'loans')[0];p.loans_enabled=False
    with pytest.raises(ProviderError) as e:await p.get_loan(a,l)
    assert e.value.code=='unsupported';assert (await p.list_cards(a)).items
    p.loans_enabled=True
    with psycopg.connect(w.admin_dsn) as c:
        c.execute('LOCK TABLE banking.loans IN ACCESS EXCLUSIVE MODE');start=time.monotonic()
        with pytest.raises(ProviderError) as e:await p.get_loan(a,l)
        assert e.value.code=='unavailable' and time.monotonic()-start<3

@pytest.mark.asyncio
@pytest.mark.parametrize('args',[dict(start='2026-02-01',end='2026-01-01'),dict(start='2025-01-01',end='2026-01-31'),dict(start='bad'),dict(limit=0),dict(limit=True),dict(status='paid')])
async def test_invalid_payment_inputs(loans,args):
    p,(a,_),_,w=loans
    with pytest.raises(ProviderError):await p.list_loan_payments(a,ids(w,'loans')[0],**args)

@pytest.mark.asyncio
async def test_apr_source_and_dto_reject_rounding_and_float(loans):
    p,(a,_),_,w=loans;r=await p.get_loan(a,ids(w,'loans')[0]);data=r.model_dump()
    for value in [12.34567890,'NaN','1e-8','100.00000001','0.123456789','-1','Infinity']:
        with pytest.raises(ValidationError):Loan.model_validate({**data,'apr_percent':value})
    with psycopg.connect(w.admin_dsn) as c:
        for value in ['NaN','101','0.123456789']:
            with pytest.raises(psycopg.errors.CheckViolation):
                with c.transaction():c.execute('UPDATE banking.loans SET apr_percent=%s WHERE id=%s',(value,ids(w,'loans')[0]))
