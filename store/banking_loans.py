"""Owned loan reads; schedules and payment records have independent queries."""
from datetime import date, datetime, time, timedelta, timezone
import hashlib
from store.banking_contracts import Source, ProviderError
from store.banking_loan_contracts import Loan, LoanPage, EMIInstallment, LoanPayment, EMISchedulePage, LoanPaymentPage


class LoanReads:
    def _loans_enabled(self):
        if not self.loans_enabled: raise ProviderError('unsupported',503)

    def _loan(self,row):
        return Loan(id=str(row['id']),display_name=row['display_name'],status=row['status'],
            principal=self._money(row['principal_minor'],row['currency']),apr_percent=format(row['apr_percent'],'f'),
            monthly_payment=self._money(row['monthly_payment_minor'],row['currency']),source=self._source(row))

    async def _owned_loan(self,c,context,loan_id):
        row=await (await c.execute('SELECT * FROM banking.loans WHERE id=%s AND customer_id=%s',(loan_id,context.customer_id))).fetchone()
        if not row: raise ProviderError('not_found',404)
        return row

    async def get_loan(self,context,loan_id):
        self._loans_enabled();loan_id=self._id(loan_id)
        async with self._read(context) as c:return self._loan(await self._owned_loan(c,context,loan_id))

    async def list_loans(self,context,*,limit=20,cursor=None):
        self._loans_enabled();limit=self._limit(limit)
        async with self._read(context) as c:
            meta=await (await c.execute("""SELECT coalesce(string_agg(id::text||':'||version::text,',' ORDER BY id),'') AS versions,
                coalesce(max(as_of),transaction_timestamp()) AS as_of FROM banking.loans WHERE customer_id=%s""",(context.customer_id,))).fetchone()
            version=hashlib.sha256(meta['versions'].encode()).hexdigest();binding=dict(op='loans',owner=context.customer_id,limit=limit)
            last=self._cursor(cursor,binding,version)
            if last is not None:self._id(last)
            rows=await (await c.execute('SELECT * FROM banking.loans WHERE customer_id=%s AND (%s::uuid IS NULL OR id>%s::uuid) ORDER BY id LIMIT %s',
                (context.customer_id,last,last,limit+1))).fetchall()
            more=len(rows)>limit;rows=rows[:limit]
            return LoanPage(items=[self._loan(r) for r in rows],complete=not more,
                next_cursor=self._next(binding,version,str(rows[-1]['id'])) if more else None,source=Source(as_of=meta['as_of'],version=version))

    async def list_emi_schedule(self,context,loan_id,*,start=None,end=None,limit=20,cursor=None):
        self._loans_enabled();loan_id=self._id(loan_id);limit=self._limit(limit);first,last_day=self._range(start,end)
        async with self._read(context) as c:
            loan=await self._owned_loan(c,context,loan_id)
            binding=dict(op='emi_schedule',owner=context.customer_id,loan=loan_id,start=first.isoformat(),end=last_day.isoformat(),limit=limit)
            last=self._cursor(cursor,binding,str(loan['version']))
            try:
                if last is not None:
                    if type(last) is not list or len(last)!=2:raise ValueError()
                    at=date.fromisoformat(last[0]);self._id(last[1])
                else:at=None
            except (ValueError,TypeError):raise ProviderError('invalid_cursor',422) from None
            rows=await (await c.execute('''SELECT * FROM banking.emi_schedule WHERE customer_id=%s AND loan_id=%s
                AND due_on BETWEEN %s AND %s AND (%s::date IS NULL OR (due_on,id)>(%s::date,%s::uuid)) ORDER BY due_on,id LIMIT %s''',
                (context.customer_id,loan_id,first,last_day,at,at,last[1] if last else None,limit+1))).fetchall()
            more=len(rows)>limit;rows=rows[:limit]
            return EMISchedulePage(loan_id=loan_id,items=[EMIInstallment(id=str(r['id']),loan_id=loan_id,
                installment_number=r['installment_number'],due_on=r['due_on'],scheduled_amount=self._money(r['scheduled_minor'],loan['currency']),source=self._source(r)) for r in rows],
                complete=not more,source=self._source(loan),start=first,end=last_day,
                next_cursor=self._next(binding,str(loan['version']),[rows[-1]['due_on'].isoformat(),str(rows[-1]['id'])]) if more else None)

    async def list_loan_payments(self,context,loan_id,*,installment_id=None,start=None,end=None,status=None,limit=20,cursor=None):
        self._loans_enabled();loan_id=self._id(loan_id);limit=self._limit(limit);first,last_day=self._range(start,end)
        if installment_id is not None:installment_id=self._id(installment_id)
        if status is not None and (type(status) is not str or status not in ('declined','pending','posted','reversed')):raise ProviderError('invalid_input',422)
        async with self._read(context) as c:
            loan=await self._owned_loan(c,context,loan_id)
            if installment_id is not None:
                found=await (await c.execute('SELECT id FROM banking.emi_schedule WHERE id=%s AND loan_id=%s AND customer_id=%s',(installment_id,loan_id,context.customer_id))).fetchone()
                if not found:raise ProviderError('not_found',404)
            binding=dict(op='loan_payments',owner=context.customer_id,loan=loan_id,installment=installment_id,start=first.isoformat(),end=last_day.isoformat(),status=status,limit=limit)
            last=self._cursor(cursor,binding,str(loan['version']))
            try:
                if last is not None:
                    if type(last) is not list or len(last)!=2:raise ValueError()
                    at=datetime.fromisoformat(last[0]);self._id(last[1])
                    if at.tzinfo is None:raise ValueError()
                else:at=None
            except (ValueError,TypeError):raise ProviderError('invalid_cursor',422) from None
            lower=datetime.combine(first,time.min,tzinfo=timezone.utc);upper=datetime.combine(last_day+timedelta(days=1),time.min,tzinfo=timezone.utc)
            rows=await (await c.execute('''SELECT * FROM banking.loan_payments WHERE customer_id=%s AND loan_id=%s
                AND (%s::uuid IS NULL OR installment_id=%s::uuid) AND occurred_at>=%s AND occurred_at<%s
                AND (%s::text IS NULL OR status=%s) AND (%s::timestamptz IS NULL OR (occurred_at,id)>(%s::timestamptz,%s::uuid))
                ORDER BY occurred_at,id LIMIT %s''',(context.customer_id,loan_id,installment_id,installment_id,lower,upper,status,status,at,at,last[1] if last else None,limit+1))).fetchall()
            more=len(rows)>limit;rows=rows[:limit]
            return LoanPaymentPage(loan_id=loan_id,installment_id=installment_id,items=[LoanPayment(id=str(r['id']),loan_id=loan_id,
                installment_id=str(r['installment_id']) if r['installment_id'] else None,amount=self._money(r['amount_minor'],loan['currency']),status=r['status'],
                occurred_at=r['occurred_at'],reason=r['reason'],source=self._source(r)) for r in rows],complete=not more,source=self._source(loan),start=first,end=last_day,
                next_cursor=self._next(binding,str(loan['version']),[rows[-1]['occurred_at'].isoformat(),str(rows[-1]['id'])]) if more else None)
