"""T05.2 exact-preview confirmation. No model tool or remote side effect."""
import hashlib,json
from fastapi import HTTPException
from pydantic import field_validator
from store.banking_support import Support,PreviewBinding,Draft
from store.banking_turns import Selection
from store.banking_preferences import binding

class Confirmation(PreviewBinding):
    operation_key:str
    _key=field_validator('operation_key')(Selection.canonical.__func__)

class ExistingCase(HTTPException):
    """Only already-authorized case references may cross this typed boundary."""

class ConfirmationService(Support):
    async def confirm(self,context,proposal_id,request):
        request_hash=hashlib.sha256(json.dumps({'proposal_id':proposal_id,**request.model_dump(exclude={'operation_key'})},sort_keys=True,separators=(',',':')).encode()).hexdigest()
        async with self.database.session(context) as c:
            await c.execute("SET LOCAL statement_timeout='3s'")
            first=await self.row(c,context,proposal_id)
            await self.source(c,context,first['source_turn_id'])
            row=await self.row(c,context,proposal_id)
            await self.resource(c,Draft.model_validate(row['payload']))
            await self.database.sessions.revalidate(context)
            op=await (await c.execute('''SELECT *,expires_at<=clock_timestamp() AS expired FROM banking.support_operations
                WHERE customer_id=%s AND session_binding=%s AND operation_key=%s''',
                (context.customer_id,binding(context),request.operation_key))).fetchone()
            if op:
                if op['request_hash']!=request_hash:raise HTTPException(409,'Operation key belongs to a different request')
                if op['expired']:raise ExistingCase(409,{'code':'replay_expired','case_id':str(op['case_id']),'case_number':op['receipt']['case_number']})
                return op['receipt']
            if row['status']=='consumed':
                existing=await (await c.execute('SELECT id,case_number FROM banking.support_cases WHERE proposal_id=%s',(proposal_id,))).fetchone()
                raise ExistingCase(409,{'code':'already_submitted','case_id':str(existing['id']),'case_number':existing['case_number']})
            self.check(row,request)
            # Session is checked again after lock waits, before any side effect.
            await self.database.sessions.revalidate(context)
            value=await (await c.execute('SELECT banking.commit_support_case(%s,%s,%s,%s,%s,%s) AS receipt',
                (proposal_id,request.version,request.payload_hash,binding(context),request.operation_key,request_hash))).fetchone()
            return value['receipt']
