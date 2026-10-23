"""Fresh, session-scoped PostgreSQL account reads with authenticated keyset cursors."""
import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import json
import time
from fastapi import HTTPException
import psycopg
from pydantic import ValidationError
from store.banking_contracts import (Account, AccountPage, Transaction, TransactionPage,
    Money, Source, ProviderError, CURRENCY_EXPONENTS, canonical_uuid)
from store.banking_cards import CardReads
from store.banking_loans import LoanReads
from store.banking_policy_answers import PolicyReads

def _json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':')).encode()

def _b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')

class AccountProvider(CardReads, LoanReads, PolicyReads):
    def __init__(self, database, cursor_key: bytes, *, clock=None, cards_enabled=False, loans_enabled=False, policies_enabled=False):
        if not isinstance(cursor_key,bytes) or len(cursor_key)<32:
            raise ValueError('Cursor key requires at least 32 bytes')
        self.database=database
        self._key=cursor_key
        self._clock=clock or (lambda: datetime.now(timezone.utc))
        self.cards_enabled=cards_enabled
        self.loans_enabled=loans_enabled
        self.policies_enabled=policies_enabled

    @asynccontextmanager
    async def _read(self,context):
        try:
            async with asyncio.timeout(2):
                async with self.database.session(context,readonly=True) as c:
                    await c.execute("SET LOCAL statement_timeout='1800ms'")
                    yield c
        except HTTPException as error:
            raise ProviderError('unavailable' if error.status_code==503 else 'unauthenticated',error.status_code,error.status_code==503) from None
        except (psycopg.Error,TimeoutError):
            raise ProviderError('unavailable',503,True) from None
        except (ValidationError,ValueError,OverflowError):
            raise ProviderError('invalid_source',503,False) from None

    def _id(self,value):
        try: return canonical_uuid(value)
        except (ValueError,TypeError,AttributeError): raise ProviderError('invalid_input',422) from None

    def _limit(self,value):
        if type(value) is not int or not 1<=value<=100: raise ProviderError('invalid_input',422)
        return value

    def _range(self,start,end):
        today=self._clock().astimezone(timezone.utc).date()
        try:
            def parse(value):
                if type(value) is not str or date.fromisoformat(value).isoformat()!=value: raise ValueError()
                return date.fromisoformat(value)
            last=today if end is None else parse(end)
            first=last-timedelta(days=29) if start is None else parse(start)
            if not 0<=(last-first).days<366: raise ValueError()
            return first,last
        except (ValueError,TypeError,OverflowError): raise ProviderError('invalid_range',422) from None

    def _cursor(self,token,binding,version):
        if token is None: return None
        try:
            if type(token) is not str or len(token)>4096: raise ValueError()
            payload,signature=token.split('.')
            expected=_b64(hmac.digest(self._key,payload.encode(),'sha256'))
            if not hmac.compare_digest(signature,expected): raise ValueError()
            value=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
            if value['binding']!=binding or type(value['exp']) is not int or value['exp']<=time.time(): raise ValueError()
            if value['version']!=version: raise ProviderError('stale_cursor',409)
            return value['last']
        except ProviderError: raise
        except (ValueError,TypeError,KeyError,UnicodeError): raise ProviderError('invalid_cursor',422) from None

    def _next(self,binding,version,last):
        payload=_b64(_json(dict(binding=binding,version=version,last=last,exp=int(time.time())+900)))
        return payload+'.'+_b64(hmac.digest(self._key,payload.encode(),'sha256'))

    def _money(self,value,currency):
        return Money(minor_units=str(value),currency=currency,exponent=CURRENCY_EXPONENTS.get(currency,-1))

    def _source(self,row):
        return Source(as_of=row['as_of'],version=str(row['version']))

    def _account(self,row):
        return Account(id=str(row['id']),display_name=row['display_name'],
            current_balance=self._money(row['current_minor'],row['currency']),
            available_balance=self._money(row['available_minor'],row['currency']),source=self._source(row))

    async def _owned(self,c,context,account_id):
        row=await (await c.execute('SELECT * FROM banking.accounts WHERE id=%s AND customer_id=%s',(account_id,context.customer_id))).fetchone()
        if not row: raise ProviderError('not_found',404)
        return row

    async def get_account(self,context,account_id):
        account_id=self._id(account_id)
        async with self._read(context) as c:
            return self._account(await self._owned(c,context,account_id))

    async def list_accounts(self,context,*,limit=20,cursor=None):
        limit=self._limit(limit)
        async with self._read(context) as c:
            # Aggregate only owned versions, preserving a bounded response and detecting changed pages.
            meta=await (await c.execute("""SELECT coalesce(string_agg(id::text||':'||version::text,',' ORDER BY id),'') AS versions,
                coalesce(max(as_of),transaction_timestamp()) AS as_of FROM banking.accounts WHERE customer_id=%s""",(context.customer_id,))).fetchone()
            version=hashlib.sha256(meta['versions'].encode()).hexdigest()
            binding=dict(op='accounts',owner=context.customer_id,limit=limit)
            last=self._cursor(cursor,binding,version)
            if last is not None: self._id(last)
            rows=await (await c.execute('SELECT * FROM banking.accounts WHERE customer_id=%s AND (%s::uuid IS NULL OR id>%s::uuid) ORDER BY id LIMIT %s',
                (context.customer_id,last,last,limit+1))).fetchall()
            more=len(rows)>limit; rows=rows[:limit]
            return AccountPage(items=[self._account(row) for row in rows],complete=not more,
                next_cursor=self._next(binding,version,str(rows[-1]['id'])) if more else None,
                source=Source(as_of=meta['as_of'],version=version))

    async def list_transactions(self,context,account_id,*,start=None,end=None,status=None,limit=20,cursor=None):
        account_id=self._id(account_id); limit=self._limit(limit); first,last_date=self._range(start,end)
        if status is not None and (type(status) is not str or status not in ('declined','pending','posted','reversed')):
            raise ProviderError('invalid_input',422)
        async with self._read(context) as c:
            account=await self._owned(c,context,account_id)
            binding=dict(op='transactions',owner=context.customer_id,account=account_id,start=first.isoformat(),end=last_date.isoformat(),status=status,limit=limit)
            last=self._cursor(cursor,binding,str(account['version']))
            if last is not None:
                if type(last) is not list or len(last)!=2: raise ProviderError('invalid_cursor',422)
                try: day=date.fromisoformat(last[0]); self._id(last[1])
                except (ValueError,TypeError): raise ProviderError('invalid_cursor',422) from None
            else: day=None
            rows=await (await c.execute('''SELECT * FROM banking.account_transactions
                WHERE customer_id=%s AND account_id=%s AND booked_on BETWEEN %s AND %s
                AND (%s::text IS NULL OR status=%s) AND (%s::date IS NULL OR (booked_on,id)>(%s::date,%s::uuid))
                ORDER BY booked_on,id LIMIT %s''',(context.customer_id,account_id,first,last_date,status,status,day,day,last[1] if last else None,limit+1))).fetchall()
            more=len(rows)>limit; rows=rows[:limit]
            return TransactionPage(items=[Transaction(id=str(r['id']),account_id=str(r['account_id']),
                amount=self._money(r['amount_minor'],account['currency']),status=r['status'],booked_on=r['booked_on'],reason=r['reason'],source=self._source(r)) for r in rows],
                complete=not more,next_cursor=self._next(binding,str(account['version']),[rows[-1]['booked_on'].isoformat(),str(rows[-1]['id'])]) if more else None,
                source=self._source(account),start=first,end=last_date)
