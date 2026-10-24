"""User-friendly read surface. Identity, CSRF and provider ownership stay server-side."""
import asyncio
from typing import Literal
from uuid import UUID
from fastapi import Depends,HTTPException
from api.foundation import EmptyRequest
from api.preferences import Capture
from store.banking_turns import TurnRequest
from store.banking_preferences import Preferences
from store.preference_grammar import extract
from store.banking_workspace import Workspace
from jobs.banking_consumer import Consumer


def install(app,database,provider,settings,principal,mutation,*,enabled=False):
    service=Workspace(database,provider);app.state.workspace=service
    def available():
        if not enabled:raise HTTPException(503)
    @app.post('/api/v1/workspace/prepare')
    async def prepare(body:EmptyRequest,context=Depends(mutation)):
        available();return await service.prepare(context)
    @app.post('/api/v1/workspace/new')
    async def new(body:EmptyRequest,context=Depends(mutation)):
        available();conversation=await database.create_conversation(context)
        return await service.prepare(context,conversation['id'])
    @app.get('/api/v1/products/{kind}')
    async def products(kind:Literal['account','card','loan'],cursor:str|None=None,context=Depends(principal)):
        available();return await service.products(context,kind,cursor=cursor)
    @app.get('/api/v1/conversations/{cid}/preference-messages')
    async def preferences(cid:UUID,context=Depends(principal)):
        available()
        # Same receipt-gated recovery as the existing preferences endpoint.
        pref=Preferences(database)
        consumer=Consumer('',settings.worker_dsn)
        for work in await pref.pending(context):
            await asyncio.to_thread(consumer.run_preference,context.customer_id,work['event_id'])
        return await service.preference_history(context,cid)
    @app.post('/api/v1/conversations/{cid}/messages',status_code=202)
    async def message(cid:UUID,body:TurnRequest,context=Depends(mutation)):
        available()
        if extract(body.text,cid) and not body.resource_selection and not body.action:
            result=await Preferences(database).accept(context,Capture(client_event_id=UUID(body.client_turn_id),conversation_id=cid,text=body.text))
            result.pop('outbox_id',None)
            return dict(kind='preference',**result)
        if not app.state.turn_acceptance_enabled:raise HTTPException(503)
        result=await app.state.turns.accept(context,str(cid),body.model_copy(update={'workspace':True}))
        return dict(kind='turn',**result)
