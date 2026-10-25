"""Authenticated read reconciliation; creation receipts never stand in for current status."""
import hashlib,json
from uuid import UUID,uuid5
from fastapi import HTTPException
import psycopg
from store.banking_confirmation import ConfirmationService
from store.banking_support import Draft
from store.banking_preferences import binding

def digest(proposal_id,request):
    return hashlib.sha256(json.dumps({'proposal_id':proposal_id,**request.model_dump(exclude={'operation_key'})},sort_keys=True,separators=(',',':')).encode()).hexdigest()

def case_public(row):
    return {k:row[k] for k in ('id','case_number','status','created_at','status_updated_at','checked_at')}

class Continuity(ConfirmationService):
    async def submit(self,context,proposal_id,request):
        try:
            async with self.database.session(context) as c:
                await c.execute("SET LOCAL statement_timeout='3s'")
                first=await self.row(c,context,proposal_id)
                await self.source(c,context,first['source_turn_id'])
                row=await self.row(c,context,proposal_id)
                await self.resource(c,Draft.model_validate(row['payload']))
                await self.database.sessions.revalidate(context)
                if row['status']!='consumed':
                    self.check(row,request)
                    await c.execute('SELECT banking.reserve_support_submission(%s,%s,%s,%s,%s,%s)',
                        (proposal_id,request.version,request.payload_hash,binding(context),request.operation_key,digest(proposal_id,request)))
            # A durable intent is not success. The five T05.2 effect records
            # still commit together in the existing independent transaction.
            return await self.confirm(context,proposal_id,request)
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409,'Original request required') from None

    async def outcome(self,context,proposal_id,request=None):
        async with self.database.session(context) as c:
            await c.execute("SET LOCAL statement_timeout='3s'")
            first=await self.row(c,context,proposal_id)
            await self.source(c,context,first['source_turn_id'])
            row=await self.row(c,context,proposal_id)
            await self.resource(c,Draft.model_validate(row['payload']))
            await self.database.sessions.revalidate(context)
            intent=await (await c.execute('SELECT * FROM banking.support_submission_intents WHERE proposal_id=%s AND session_binding=%s',(proposal_id,binding(context)))).fetchone()
            if request:
                if request.version!=row['version'] or request.payload_hash!=row['payload_hash']:raise HTTPException(409,'Preview binding changed')
                if intent and (str(intent['operation_key'])!=request.operation_key or intent['request_hash']!=digest(proposal_id,request)):raise HTTPException(409,'Original request required')
            case=await (await c.execute('SELECT *,clock_timestamp() AS checked_at FROM banking.support_cases WHERE proposal_id=%s',(proposal_id,))).fetchone()
            original={'proposal_id':proposal_id,'operation_key':str(intent['operation_key']) if intent else str(uuid5(UUID(proposal_id),row['payload_hash'])),
                      'version':row['version'],'payload_hash':row['payload_hash']}
            result={'outcome':'committed' if case else 'not_recorded','accepted':bool(intent),'request':original,
                    'proposal_status':row['status'],'creation_receipt':None,'current_case':case_public(case) if case else None}
            if case:
                op=await (await c.execute('SELECT receipt,expires_at>clock_timestamp() AS retained FROM banking.support_operations WHERE case_id=%s AND session_binding=%s',(case['id'],binding(context)))).fetchone()
                if op and op['retained']:result['creation_receipt']=op['receipt']
            elif row['status']!='pending':result.update(outcome='not_submitted',request=None)
            return result

    async def cases(self,context,cursor=None,limit=20):
        async with self.database.session(context,readonly=True) as c:
            anchor=None
            if cursor:
                try:
                    if str(UUID(cursor))!=cursor:raise ValueError()
                except ValueError:raise HTTPException(404,'Not found') from None
                anchor=await (await c.execute('SELECT created_at,id FROM banking.support_cases WHERE id=%s',(cursor,))).fetchone()
                if not anchor:raise HTTPException(404,'Not found')
            rows=await (await c.execute('''SELECT *,clock_timestamp() AS checked_at FROM banking.support_cases
                WHERE (%s::timestamptz IS NULL OR (created_at,id)<(%s::timestamptz,%s::uuid))
                ORDER BY created_at DESC,id DESC LIMIT %s''',(anchor['created_at'] if anchor else None,anchor['created_at'] if anchor else None,anchor['id'] if anchor else None,limit+1))).fetchall()
            return {'items':[case_public(r) for r in rows[:limit]],'next_cursor':str(rows[limit-1]['id']) if len(rows)>limit else None}

    async def case(self,context,case_id):
        try:
            if str(UUID(case_id))!=case_id:raise ValueError()
        except ValueError:raise HTTPException(404,'Not found') from None
        async with self.database.session(context,readonly=True) as c:
            row=await (await c.execute('SELECT *,clock_timestamp() AS checked_at FROM banking.support_cases WHERE id=%s',(case_id,))).fetchone()
            if not row:raise HTTPException(404,'Not found')
            return case_public(row)
