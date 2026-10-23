"""Customer/session working context and optional governed lexical history."""
from fastapi import HTTPException
import psycopg
from psycopg.types.json import Jsonb
from store.banking_preferences import binding


class ContextMemory:
    def __init__(self, database):
        self.database = database

    async def call(self, context, query, args):
        try:
            async with self.database.session(context) as c:
                return (await (await c.execute(query, args)).fetchone())['result']
        except (psycopg.errors.SerializationFailure, psycopg.errors.UniqueViolation):
            raise HTTPException(409, 'Version or idempotency conflict') from None
        except psycopg.errors.ObjectNotInPrerequisiteState:
            raise HTTPException(410, 'Source deleted') from None
        except psycopg.errors.NoDataFound:
            raise HTTPException(404, 'Resource not found') from None

    async def read_context(self, context, conversation):
        await self.database.conversation(context, conversation)
        async with self.database.session(context) as c:
            row = await (await c.execute('SELECT version,selections FROM banking.working_context WHERE customer_id=%s AND session_binding=%s AND conversation_id=%s AND expires_at>clock_timestamp()',
                (context.customer_id, binding(context), conversation))).fetchone()
        return dict(conversation_id=str(conversation), **(row or dict(version=0, selections=[])))

    async def write_context(self, context, conversation, body):
        await self.database.conversation(context, conversation)
        return await self.call(context, 'SELECT banking.write_context(%s,%s,%s,%s) AS result',
            (binding(context), conversation, body.expected_version, Jsonb(body.selections)))

    async def capture(self, context, body):
        await self.database.conversation(context, body.conversation_id)
        return await self.call(context, 'SELECT banking.create_memory(%s,%s,%s) AS result',
            (body.client_event_id, body.conversation_id, body.text))

    async def derive(self, context, body):
        await self.database.conversation(context, body.conversation_id)
        return await self.call(context, 'SELECT banking.derive_memory(%s,%s,%s,%s) AS result',
            (body.source_ids, body.kind, body.conversation_id, body.text))

    async def read(self, context, conversation=None, query=None, memory_id=None):
        if conversation:
            await self.database.conversation(context, conversation)
        async with self.database.session(context) as c:
            rows = await (await c.execute('SELECT * FROM banking.read_memories(%s,%s,%s)',
                (conversation, query, memory_id))).fetchall()
        for row in rows:
            row['source_refs'] = row.pop('source_ids')
            row['authority'] = 'historical_user_memory_not_current_bank_facts'
        if memory_id:
            if not rows:
                raise HTTPException(404, 'Resource not found')
            return rows[0]
        return dict(items=rows, lane_status='lexical_history' if query is not None else 'history_listing',
                    semantic_status='disabled_no_embedding_provider', authoritative_bank_facts=False)

    async def erase(self, context, source):
        return await self.call(context, 'SELECT banking.erase_memory_source(%s) AS result', (source,))

    async def restore(self, context, source):
        return await self.call(context, 'SELECT banking.restore_memory_check(%s) AS result', (source,))

    async def retention(self, context):
        return await self.call(context, 'SELECT banking.memory_retention() AS result', ())
