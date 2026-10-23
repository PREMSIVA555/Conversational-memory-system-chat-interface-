"""Session-bound exact settings and ordered proposals; never semantic retrieval."""
from hashlib import sha256
from uuid import UUID
from fastapi import HTTPException
import psycopg
from psycopg.types.json import Jsonb
from store.preference_grammar import extract, GRAMMAR

DEFAULTS={'preferred_response_language':'en','preferred_response_format':'concise'}

def binding(context):
    return sha256(context.session_key.encode()).hexdigest()

class Preferences:
    def __init__(self,database):
        self.database=database

    async def rollout(self,context):
        async with self.database.session(context) as c:
            row=await (await c.execute('SELECT new_writes_enabled,generation,legacy_guard_attestation FROM banking.memory_rollout WHERE singleton')).fetchone()
        return {**row,'legacy_preference_writer':'python_guarded',
                'reader':'accepted_source_projection_only','rollback':'preserve_events_and_tombstones'}

    async def pending(self,context):
        async with self.database.session(context) as c:
            return await (await c.execute('SELECT event_id FROM banking.pending_preference_work()')).fetchall()

    async def accept(self,context,body,manual=False):
        conversation=body.conversation_id
        if conversation:
            await self.database.conversation(context,conversation)
        if manual:
            scope='global' if body.scope=='global' else 'conversation:'+str(conversation)
            proposals=[dict(key=body.key,value=body.value,operation=body.operation,scope_key=scope)]
            payload=dict(handler='preferences.v1',key=body.key,scope_key=scope,operation=body.operation,
                         value=body.value,expected_version=body.expected_version,proposals=proposals)
        else:
            proposals=extract(body.text,conversation)
            payload=dict(handler='preferences.v1',text=body.text,conversation_id=str(conversation) if conversation else None)
        try:
            async with self.database.session(context) as c:
                row=await (await c.execute('SELECT banking.accept_preference(%s,%s,%s,%s,%s,%s) AS id',
                    (body.client_event_id,'panel' if manual else 'chat',conversation,Jsonb(payload),binding(context),Jsonb(proposals)))).fetchone()
                result=await (await c.execute('SELECT p.source_id AS event_id,p.status,p.proposals,s.sequence AS source_sequence,banking.preference_outbox(s.id) AS outbox_id FROM banking.preference_proposals p JOIN banking.accepted_user_events s ON s.id=p.source_id WHERE p.source_id=%s',(row['id'],))).fetchone()
        except (psycopg.errors.SerializationFailure,psycopg.errors.UniqueViolation):
            raise HTTPException(409,'Preference conflict') from None
        except psycopg.errors.ObjectNotInPrerequisiteState:
            raise HTTPException(503,'Preference writes unavailable') from None
        return result

    async def read(self,context,conversation_id=None,response_event_id=None):
        if conversation_id:
            await self.database.conversation(context,conversation_id)
        async with self.database.session(context) as c:
            projections=await (await c.execute('SELECT key,scope_key,value,tombstoned,version,last_sequence FROM banking.preferences_current WHERE customer_id=%s AND (scope_key=%s OR scope_key=%s) ORDER BY last_sequence',
                (context.customer_id,'global','conversation:'+str(conversation_id)))).fetchall()
            proposals=await (await c.execute("SELECT p.source_id AS event_id,CASE WHEN w.status='dead_letter' THEN 'failed' ELSE p.status END AS status,p.proposals,coalesce(p.failure_code,w.failure_code) AS failure_code,s.sequence AS source_sequence,s.conversation_id FROM banking.preference_proposals p JOIN banking.accepted_user_events s ON s.id=p.source_id LEFT JOIN LATERAL banking.preference_work_status(s.id) w ON true WHERE p.customer_id=%s AND p.session_binding=%s AND (s.conversation_id IS NULL OR s.conversation_id=%s) ORDER BY s.sequence DESC LIMIT 32",
                (context.customer_id,binding(context),conversation_id))).fetchall()
        effective={key:dict(value=value,scope='default',status='default',source_sequence=0) for key,value in DEFAULTS.items()}
        candidates={}
        watermarks={(p['key'],p['scope_key']):p['last_sequence'] for p in projections}
        for p in projections:
            if not p['tombstoned']:
                candidates[(p['key'],p['scope_key'])]=dict(value=p['value'],scope=p['scope_key'],status='persisted',source_sequence=p['last_sequence'])
        for event in reversed(proposals):
            if event['status'] not in ('pending','temporary'):
                continue
            for proposal in event['proposals']:
                scope=proposal['scope_key']; key=proposal['key']
                if scope.startswith('response:') and str(event['event_id'])!=str(response_event_id):
                    continue
                if scope not in ('global','conversation:'+str(conversation_id),'response:'+str(conversation_id)):
                    continue
                if watermarks.get((key,scope),0)>=event['source_sequence']:
                    continue
                watermarks[(key,scope)]=event['source_sequence']
                if proposal['operation']=='DELETE': candidates.pop((key,scope),None)
                else: candidates[(key,scope)]=dict(value=proposal['value'],scope=scope,status=event['status'],source_sequence=event['source_sequence'])
        for scope in ('global','conversation:'+str(conversation_id),'response:'+str(conversation_id)):
            for key in DEFAULTS:
                if (key,scope) in candidates: effective[key]=candidates[(key,scope)]
        return dict(effective=effective,projections=projections,proposals=proposals,grammar=GRAMMAR,
                    rollout=await self.rollout(context))
