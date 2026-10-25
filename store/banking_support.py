"""Immutable session-bound previews. No case write or model confirmation capability."""
import hashlib
import json
import unicodedata
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field, field_validator, model_validator
from psycopg.types.json import Jsonb
from store.banking_preferences import binding
from store.banking_turns import Closed, Selection


class Draft(Closed):
    source_turn_id: str
    topic: Literal['general', 'account', 'card', 'loan']
    resource_id: str | None = None
    summary: str = Field(min_length=1, max_length=1000)

    _canonical = field_validator('source_turn_id')(Selection.canonical.__func__)

    @field_validator('summary')
    @classmethod
    def normalize(cls, value):
        value = unicodedata.normalize('NFC', value.replace('\r\n', '\n').replace('\r', '\n')).strip()
        if not value or any(ord(c) < 32 and c not in '\n\t' for c in value):
            raise ValueError('Invalid summary')
        return value

    @model_validator(mode='after')
    def resource(self):
        if self.topic == 'general':
            if self.resource_id is not None: raise ValueError('General request has no resource')
        elif self.resource_id is None or str(UUID(self.resource_id)) != self.resource_id:
            raise ValueError('Canonical resource required')
        return self


class PreviewBinding(Closed):
    version: int = Field(ge=1)
    payload_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


class Edit(PreviewBinding):
    draft: Draft


def public(row, resource_label=None):
    return {**{k: row[k] for k in ('id', 'source_turn_id', 'version', 'payload', 'payload_hash', 'created_at', 'expires_at', 'status')},
            'resource_label': resource_label}


class Support:
    def __init__(self, database):
        self.database = database

    async def source(self, c, context, turn_id):
        # The definer helper takes the same customer lock as source erasure,
        # then checks ownership/session/ledger with a fresh statement snapshot.
        # Runtime still receives no UPDATE privilege on turns or customers.
        row = await (await c.execute('SELECT banking.lock_support_source(%s,%s) AS ok',
                                     (turn_id, binding(context)))).fetchone()
        if not row['ok']: raise HTTPException(404, 'Not found')
        await c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,51))", (str(turn_id),))

    async def resource(self, c, draft):
        if draft.topic == 'general': return
        table = {'account': 'accounts', 'card': 'cards', 'loan': 'loans'}[draft.topic]
        # Table name is an internal constant; RLS enforces the trusted owner context.
        columns = 'display_name,kind,last_four' if draft.topic == 'card' else 'display_name'
        row = await (await c.execute(f'SELECT {columns} FROM banking.{table} WHERE id=%s', (draft.resource_id,))).fetchone()
        if not row: raise HTTPException(404, 'Not found')
        return row['display_name'] + (f" · {row['kind']} •••• {row['last_four']}" if draft.topic == 'card' else '')

    async def row(self, c, context, proposal_id):
        try:
            if str(UUID(proposal_id)) != proposal_id: raise ValueError()
        except ValueError: raise HTTPException(404, 'Not found') from None
        row = await (await c.execute('''SELECT p.*,CASE WHEN p.status='pending' AND p.expires_at<=clock_timestamp()
            THEN 'expired' ELSE p.status END AS status FROM banking.support_proposals p
            JOIN banking.turns t ON t.id=p.source_turn_id
            WHERE p.id=%s AND p.session_binding=%s
              AND NOT EXISTS(SELECT 1 FROM banking.erasure_ledger e WHERE e.source_id=t.source_id)''',
            (proposal_id, binding(context)))).fetchone()
        if not row: raise HTTPException(404, 'Not found')
        return row

    def check(self, row, expected):
        if row['status'] != 'pending' or row['version'] != expected.version or row['payload_hash'] != expected.payload_hash:
            raise HTTPException(409, 'Preview is no longer current')

    async def get(self, context, proposal_id):
        async with self.database.session(context) as c:
            row = await self.row(c, context, proposal_id)
            return public(row, await self.resource(c, Draft.model_validate(row['payload'])))

    async def latest(self, context, turn_id):
        try: turn_id = str(UUID(turn_id))
        except ValueError: raise HTTPException(404, 'Not found') from None
        async with self.database.session(context) as c:
            await self.source(c, context, turn_id)
            row = await (await c.execute('SELECT id FROM banking.support_proposals WHERE source_turn_id=%s ORDER BY version DESC LIMIT 1', (turn_id,))).fetchone()
            if not row: raise HTTPException(404, 'Not found')
            row = await self.row(c, context, str(row['id']))
            return public(row, await self.resource(c, Draft.model_validate(row['payload'])))

    async def create(self, context, draft, *, proposal_id=None, expected=None):
        async with self.database.session(context) as c:
            await c.execute("SET LOCAL statement_timeout='3s'")
            await self.source(c, context, draft.source_turn_id)
            label = await self.resource(c, draft)
            if proposal_id:
                old = await self.row(c, context, proposal_id)
                self.check(old, expected)
                if str(old['source_turn_id']) != draft.source_turn_id: raise HTTPException(409, 'Source cannot change')
                await c.execute("UPDATE banking.support_proposals SET status='cancelled' WHERE id=%s", (proposal_id,))
            else:
                old = await (await c.execute('SELECT id FROM banking.support_proposals WHERE source_turn_id=%s', (draft.source_turn_id,))).fetchone()
                if old: raise HTTPException(409, 'A preview already exists for this question')
            version = (await (await c.execute('SELECT coalesce(max(version),0)+1 AS n FROM banking.support_proposals WHERE source_turn_id=%s', (draft.source_turn_id,))).fetchone())['n']
            payload = draft.model_dump()
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
            row = await (await c.execute('''INSERT INTO banking.support_proposals
                (customer_id,source_turn_id,session_binding,version,payload,payload_hash)
                VALUES(%s,%s,%s,%s,%s,%s) RETURNING *''',
                (context.customer_id, draft.source_turn_id, binding(context), version, Jsonb(payload), digest))).fetchone()
            return public(row, label)

    async def validate(self, context, proposal_id, expected, *, cancel=False):
        async with self.database.session(context) as c:
            await c.execute("SET LOCAL statement_timeout='3s'")
            first = await self.row(c, context, proposal_id)
            await self.source(c, context, first['source_turn_id'])
            row = await self.row(c, context, proposal_id)
            self.check(row, expected)
            label = await self.resource(c, Draft.model_validate(row['payload']))
            if cancel:
                await c.execute("UPDATE banking.support_proposals SET status='cancelled' WHERE id=%s", (proposal_id,))
                row['status'] = 'cancelled'
            return public(row, label)
