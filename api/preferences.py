"""Authenticated, strict DTO boundary for preference-only user input."""
from typing import Literal
from uuid import UUID
from fastapi import BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from jobs.banking_consumer import Consumer
from store.banking_preferences import Preferences

class Capture(BaseModel):
    model_config=ConfigDict(extra='forbid')
    client_event_id: UUID
    text: str=Field(min_length=1,max_length=1000)
    conversation_id: UUID|None=None

class Manual(BaseModel):
    model_config=ConfigDict(extra='forbid')
    client_event_id: UUID
    key: Literal['preferred_response_language','preferred_response_format']
    value: str|None=None
    operation: Literal['SET','DELETE']='SET'
    scope: Literal['global','conversation']='global'
    conversation_id: UUID|None=None
    expected_version: int=Field(ge=0,strict=True)

    @model_validator(mode='after')
    def valid(self):
        if self.scope=='conversation' and not self.conversation_id:
            raise ValueError('Conversation required')
        if self.operation=='DELETE':
            if self.value is not None: raise ValueError('Delete value must be absent')
        elif self.value not in ({'en','hi'} if self.key=='preferred_response_language' else {'concise','detailed'}):
            raise ValueError('Unsupported preference')
        return self

def install(app,database,settings,principal,mutation):
    preferences=Preferences(database)
    consumer=Consumer('',settings.worker_dsn) if settings else None
    app.state.preferences=preferences

    def process(customer,event):
        try: consumer.run_preference(customer,event)
        except Exception:
            # Durable source remains pending/claimed. Authenticated polling safely
            # redelivers the same key after lease expiry; no false saved status.
            pass

    @app.get('/api/v1/preferences/rollout')
    async def rollout(context=Depends(principal)):
        return await preferences.rollout(context)

    @app.get('/api/v1/preferences')
    async def read(background_tasks:BackgroundTasks,conversation_id:UUID|None=None,response_event_id:UUID|None=None,context=Depends(principal)):
        result=await preferences.read(context,conversation_id,response_event_id)
        # Bounded recovery after app restart, with no user-requested generic worker.
        for work in await preferences.pending(context):
            background_tasks.add_task(process,context.customer_id,work['event_id'])
        return result

    @app.post('/api/v1/preferences/manual',status_code=202)
    async def manual(body:Manual,context=Depends(mutation)):
        result=await preferences.accept(context,body,manual=True)
        result.pop('outbox_id',None)
        return result

    @app.post('/api/v1/preferences/capture',status_code=202)
    async def capture(body:Capture,background_tasks:BackgroundTasks,context=Depends(mutation)):
        result=await preferences.accept(context,body)
        outbox=result.pop('outbox_id')
        background_tasks.add_task(process,context.customer_id,outbox)
        return result
