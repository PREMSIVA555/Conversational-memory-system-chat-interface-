"""Owned product discovery and source-linked resource context, never financial memory."""
from typing import Literal
from uuid import UUID
import time
from fastapi import HTTPException
from pydantic import Field, field_validator
from store.banking_preferences import binding
from store.banking_contracts import ProviderError


async def validate_selection(database, context, selection):
    table={'account':'accounts','card':'cards','loan':'loans'}[selection['kind']]
    async with database.session(context) as c:
        await c.execute("SET LOCAL statement_timeout='1800ms'")
        columns='version,display_name'+(',kind,last_four' if selection['kind']=='card' else '')
        row=await (await c.execute(f'SELECT {columns} FROM banking.{table} WHERE id=%s', (selection['id'],))).fetchone()
    if not row: raise HTTPException(404,'Not found')
    if str(row['version'])!=selection['version']:raise HTTPException(409,'Selected product changed')
    return row['display_name']+(f" · {row['kind']} •••• {row['last_four']}" if selection['kind']=='card' else '')


class Workspace:
    def __init__(self,database,provider):self.database=database;self.provider=provider

    async def products(self,context,kind,*,cursor=None):
        started=time.monotonic()
        try:
            method={'account':'list_accounts','card':'list_cards','loan':'list_loans'}[kind]
            page=await getattr(self.provider,method)(context,limit=20,cursor=cursor)
            return dict(status='ready',**page.model_dump(mode='json'),elapsed_ms=round((time.monotonic()-started)*1000,2))
        except ProviderError as error:
            if error.status==401:raise HTTPException(401) from None
            return dict(status='unavailable' if error.code!='unsupported' else 'disabled',items=[],complete=False,next_cursor=None,elapsed_ms=round((time.monotonic()-started)*1000,2))

    async def prepare(self,context,conversation_id=None):
        async with self.database.session(context) as c:
            row=await (await c.execute('SELECT banking.prepare_workspace(%s,%s) AS id',(binding(context),conversation_id))).fetchone()
        return dict(conversation_id=str(row['id']))

    async def context(self,context,cid,before_source=None):
        async with self.database.session(context,worker=True) as c:
            row=await (await c.execute('''SELECT resource,refs,lineage,expires_at FROM banking.resource_context
                WHERE customer_id=%s AND session_binding=%s AND conversation_id=%s AND expires_at>clock_timestamp() AND resource<>'null'::jsonb
                AND NOT EXISTS(SELECT 1 FROM banking.erasure_ledger WHERE source_id=ANY(lineage))
                AND (%s::uuid IS NULL OR sequence<(SELECT sequence FROM banking.accepted_user_events WHERE id=%s))''',
                (context.customer_id,binding(context),cid,before_source,before_source))).fetchone()
        return row

    async def preference_history(self,context,cid):
        await self.database.conversation(context,cid)
        async with self.database.session(context) as c:
            rows=await (await c.execute('''SELECT s.client_event_id,s.payload->>'text' AS text,s.accepted_at,p.status,p.proposals
                FROM banking.preference_proposals p JOIN banking.accepted_user_events s ON s.id=p.source_id
                WHERE s.conversation_id=%s AND p.session_binding=%s AND s.actor_ref='user'
                AND NOT EXISTS(SELECT 1 FROM banking.erasure_ledger WHERE source_id=s.id)
                ORDER BY s.sequence DESC LIMIT 30''',(cid,binding(context)))).fetchall()
        return dict(items=rows)
