"""Read-only card extension sharing the account provider's scoped reads/cursors."""
from datetime import datetime, time, timedelta, timezone
import hashlib
import re
from store.banking_contracts import Source, ProviderError
from store.banking_card_contracts import Card, CardPage, CardTransaction, CardTransactionPage


class CardReads:
    def _cards_enabled(self):
        if not self.cards_enabled:
            raise ProviderError('unsupported', 503)

    def _card(self, row):
        def optional(field):
            return None if row[field] is None else self._money(row[field], row['currency'])
        return Card(id=str(row['id']), display_name=row['display_name'], last_four=row['last_four'],
                    kind=row['kind'], status=row['status'], daily_limit=self._money(row['daily_limit_minor'], row['currency']),
                    credit_limit=optional('credit_limit_minor'), available_credit=optional('available_credit_minor'),
                    outstanding=optional('outstanding_minor'), source=self._source(row))

    async def _owned_card(self, c, context, card_id):
        row = await (await c.execute('SELECT * FROM banking.cards WHERE id=%s AND customer_id=%s',
                                    (card_id, context.customer_id))).fetchone()
        if not row:
            raise ProviderError('not_found', 404)
        return row

    async def get_card(self, context, card_id):
        self._cards_enabled(); card_id = self._id(card_id)
        async with self._read(context) as c:
            return self._card(await self._owned_card(c, context, card_id))

    async def list_cards(self, context, *, last_four=None, limit=20, cursor=None):
        self._cards_enabled(); limit = self._limit(limit)
        if last_four is not None and (type(last_four) is not str or not re.fullmatch(r'[0-9]{4}', last_four)):
            raise ProviderError('invalid_input', 422)
        async with self._read(context) as c:
            meta = await (await c.execute("""SELECT coalesce(string_agg(id::text||':'||version::text,',' ORDER BY id),'') AS versions,
                coalesce(max(as_of),transaction_timestamp()) AS as_of FROM banking.cards
                WHERE customer_id=%s AND (%s::text IS NULL OR last_four=%s)""", (context.customer_id, last_four, last_four))).fetchone()
            version = hashlib.sha256(meta['versions'].encode()).hexdigest()
            binding = dict(op='cards', owner=context.customer_id, last_four=last_four, limit=limit)
            last = self._cursor(cursor, binding, version)
            if last is not None: self._id(last)
            rows = await (await c.execute('''SELECT * FROM banking.cards WHERE customer_id=%s
                AND (%s::text IS NULL OR last_four=%s) AND (%s::uuid IS NULL OR id>%s::uuid) ORDER BY id LIMIT %s''',
                (context.customer_id, last_four, last_four, last, last, limit+1))).fetchall()
            more = len(rows)>limit; rows = rows[:limit]
            return CardPage(items=[self._card(r) for r in rows], complete=not more,
                next_cursor=self._next(binding, version, str(rows[-1]['id'])) if more else None,
                source=Source(as_of=meta['as_of'], version=version))

    async def list_card_transactions(self, context, card_id, *, start=None, end=None, status=None, limit=20, cursor=None):
        self._cards_enabled(); card_id=self._id(card_id); limit=self._limit(limit); first,end_day=self._range(start,end)
        if status is not None and (type(status) is not str or status not in ('declined','pending','posted','reversed')):
            raise ProviderError('invalid_input',422)
        async with self._read(context) as c:
            card=await self._owned_card(c,context,card_id)
            binding=dict(op='card_transactions',owner=context.customer_id,card=card_id,start=first.isoformat(),end=end_day.isoformat(),status=status,limit=limit)
            last=self._cursor(cursor,binding,str(card['version']))
            if last is not None:
                try:
                    if type(last) is not list or len(last)!=2: raise ValueError()
                    at=datetime.fromisoformat(last[0]); self._id(last[1])
                    if at.tzinfo is None: raise ValueError()
                except (ValueError,TypeError): raise ProviderError('invalid_cursor',422) from None
            else: at=None
            lower=datetime.combine(first,time.min,tzinfo=timezone.utc)
            upper=datetime.combine(end_day+timedelta(days=1),time.min,tzinfo=timezone.utc)
            rows=await (await c.execute('''SELECT * FROM banking.card_transactions WHERE customer_id=%s AND card_id=%s
                AND occurred_at >= %s AND occurred_at < %s AND (%s::text IS NULL OR status=%s)
                AND (%s::timestamptz IS NULL OR (occurred_at,id)>(%s::timestamptz,%s::uuid)) ORDER BY occurred_at,id LIMIT %s''',
                (context.customer_id,card_id,lower,upper,status,status,at,at,last[1] if last else None,limit+1))).fetchall()
            more=len(rows)>limit; rows=rows[:limit]
            return CardTransactionPage(items=[CardTransaction(id=str(r['id']),card_id=card_id,
                amount=self._money(r['amount_minor'],card['currency']),status=r['status'],occurred_at=r['occurred_at'],
                reason=r['reason'],source=self._source(r)) for r in rows],complete=not more,
                next_cursor=self._next(binding,str(card['version']),[rows[-1]['occurred_at'].isoformat(),str(rows[-1]['id'])]) if more else None,
                source=self._source(card),start=first,end=end_day)
