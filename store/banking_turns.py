"""Durable turn boundary. Canonical text is NFC, LF newlines and outer trim only."""
import hashlib
import json
import unicodedata
from uuid import UUID
from typing import Literal
import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel,ConfigDict,Field,field_validator
from fastapi import HTTPException
from store.banking_preferences import binding,Preferences

class Closed(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
class Selection(Closed):
    account_id: str
    version: str=Field(pattern=r'^[1-9][0-9]{0,18}$')
    @field_validator('account_id')
    @classmethod
    def canonical(cls,value):
        if str(UUID(value))!=value: raise ValueError('Canonical UUID required')
        return value
class Instructions(Closed):
    language: Literal['en','hi'] | None=None
    format: Literal['concise','detailed'] | None=None
class TurnRequest(Closed):
    client_turn_id: str
    text: str=Field(min_length=1,max_length=8000)
    selection: Selection | None=None
    instructions: Instructions | None=None
    _canonical=field_validator('client_turn_id')(Selection.canonical.__func__)
    @field_validator('text')
    @classmethod
    def normalize(cls,value):
        value=unicodedata.normalize('NFC',value.replace('\r\n','\n').replace('\r','\n')).strip()
        if not value: raise ValueError('Empty enquiry')
        return value

def public(row,*,replayed=False):
    cid=str(row['conversation_id']);tid=str(row['id'])
    return dict(turn_id=tid,client_turn_id=str(row['client_turn_id']),conversation_id=cid,
        text=row.get('user_text',''),user_text=row.get('user_text',''),status=row['status'],replayed=replayed,result=row['result'],
        accepted_at=row['accepted_at'].isoformat(),last_event_id=row.get('last_event_id',1),
        status_url=f'/api/v1/conversations/{cid}/turns/{tid}',events_url=f'/api/v1/conversations/{cid}/turns/{tid}/events')

class Turns:
    def __init__(self,database,*,run_seconds=10):self.database=database;self.run_seconds=run_seconds
    async def accept(self,context,cid,body):
        await self.database.conversation(context,cid)
        cid=str(UUID(cid))
        operation=dict(conversation_id=cid,text=body.text,selection=body.selection.model_dump() if body.selection else None,
                       instructions=body.instructions.model_dump(exclude_none=True) if body.instructions else {})
        fingerprint=hashlib.sha256(json.dumps(operation,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        async with self.database.session(context) as c:
            existing=await (await c.execute('SELECT id,fingerprint,session_binding FROM banking.turns WHERE client_turn_id=%s',(body.client_turn_id,))).fetchone()
        if existing:
            if existing['fingerprint']!=fingerprint or existing['session_binding']!=binding(context):raise HTTPException(409)
            result=await self.get(context,cid,turn_id=str(existing['id']));result['replayed']=True
            return result
        preferences=await Preferences(self.database).read(context,cid)
        effective={k:v['value'] for k,v in preferences['effective'].items()}
        coordinator_request=body.text
        if body.selection:
            # Version and ownership checked on acceptance and again before execution.
            try:
                async with self.database.session(context) as c:
                    # A bank read can fail before a durable turn exists. Bound this
                    # validation too, rather than leaving acceptance waiting on a lock.
                    await c.execute("SELECT set_config('statement_timeout','2000',true)")
                    selected=await (await c.execute('SELECT version FROM banking.accounts WHERE id=%s',(body.selection.account_id,))).fetchone()
            except psycopg.errors.QueryCanceled:
                raise HTTPException(503) from None
            if not selected:raise HTTPException(404)
            if str(selected['version'])!=body.selection.version:raise HTTPException(409)
            coordinator_request+='\nSelected account: '+body.selection.account_id
        request={**operation,'preferences':effective,'run_fingerprint':hashlib.sha256(coordinator_request.encode()).hexdigest()}
        try:
            async with self.database.session(context) as c:
                previous=await (await c.execute('SELECT id FROM banking.turns WHERE client_turn_id=%s',(body.client_turn_id,))).fetchone()
                row=await (await c.execute('SELECT * FROM banking.accept_turn(%s,%s,%s,%s,%s,%s,%s)',
                    (body.client_turn_id,cid,Jsonb(request),fingerprint,context.session_key,binding(context),self.run_seconds))).fetchone()
        except psycopg.errors.UniqueViolation:raise HTTPException(409) from None
        result=await self.get(context,cid,turn_id=str(row['id']))
        result['replayed']=bool(previous)
        return result
    async def get(self,context,cid,*,turn_id=None,client_id=None):
        await self.database.conversation(context,cid)
        key='id' if turn_id else 'client_turn_id'
        try:value=str(UUID(turn_id or client_id))
        except ValueError:raise HTTPException(404) from None
        async with self.database.session(context) as c:
            row=await (await c.execute(f"SELECT t.*,s.payload->>'text' AS user_text,(SELECT max(event_id) FROM banking.turn_events WHERE turn_id=t.id) AS last_event_id FROM banking.turns t JOIN banking.accepted_user_events s ON s.id=t.source_id WHERE t.conversation_id=%s AND t.{key}=%s AND t.session_binding=%s AND NOT EXISTS(SELECT 1 FROM banking.erasure_ledger WHERE source_id=t.source_id)",(cid,value,binding(context)))).fetchone()
        if not row:raise HTTPException(404)
        return public(row)
    async def list(self,context,cid):
        await self.database.conversation(context,cid)
        async with self.database.session(context) as c:
            rows=await (await c.execute("SELECT t.*,s.payload->>'text' AS user_text,(SELECT max(event_id) FROM banking.turn_events WHERE turn_id=t.id) AS last_event_id FROM banking.turns t JOIN banking.accepted_user_events s ON s.id=t.source_id WHERE t.conversation_id=%s AND t.session_binding=%s AND NOT EXISTS(SELECT 1 FROM banking.erasure_ledger WHERE source_id=t.source_id) ORDER BY accepted_at DESC,t.id DESC LIMIT 50",(cid,binding(context)))).fetchall()
        return dict(items=[public(row) for row in reversed(rows)],limit=50)
    async def events(self,context,tid,cursor):
        async with self.database.session(context) as c:
            return await (await c.execute('SELECT event_id,event_type,payload FROM banking.turn_events e WHERE turn_id=%s AND event_id>%s AND EXISTS(SELECT 1 FROM banking.turns t WHERE t.id=e.turn_id AND session_binding=%s) AND NOT EXISTS(SELECT 1 FROM banking.turns t JOIN banking.erasure_ledger l ON l.source_id=t.source_id WHERE t.id=e.turn_id) ORDER BY event_id LIMIT 100',(tid,cursor,binding(context)))).fetchall()
