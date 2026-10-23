"""Strict authenticated DTOs; user memory is never current financial truth."""
from typing import Literal
from uuid import UUID
from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from store.banking_context import ContextMemory


class ContextUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=0, strict=True)
    selections: list[str] = Field(max_length=8)

    @field_validator('selections')
    @classmethod
    def canonical(cls, selections):
        for selection in selections:
            if not selection.startswith('conversation:'):
                raise ValueError('Unsupported selection')
            value = selection.removeprefix('conversation:')
            if str(UUID(value)) != value:
                raise ValueError('Canonical conversation selection required')
        return selections


class MemoryCapture(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client_event_id: UUID
    conversation_id: UUID
    text: str = Field(min_length=1, max_length=2000)


class MemoryDerive(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_ids: list[UUID] = Field(min_length=1, max_length=8)
    kind: Literal['memory', 'summary', 'vector', 'cache', 'checkpoint']
    conversation_id: UUID
    text: str = Field(min_length=1, max_length=2000)


class Restore(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_id: UUID


def install(app, database, principal, mutation):
    memory = ContextMemory(database)
    app.state.context_memory = memory

    @app.get('/api/v1/context/{conversation_id}')
    async def read_context(conversation_id: UUID, context=Depends(principal)):
        return await memory.read_context(context, conversation_id)

    @app.put('/api/v1/context/{conversation_id}')
    async def update_context(conversation_id: UUID, body: ContextUpdate, context=Depends(mutation)):
        return await memory.write_context(context, conversation_id, body)

    @app.post('/api/v1/memory', status_code=201)
    async def capture(body: MemoryCapture, context=Depends(mutation)):
        return await memory.capture(context, body)

    @app.post('/api/v1/memory/derive', status_code=201)
    async def derive(body: MemoryDerive, context=Depends(mutation)):
        return await memory.derive(context, body)

    @app.post('/api/v1/memory/restore-check')
    async def restore(body: Restore, context=Depends(mutation)):
        return await memory.restore(context, body.source_id)

    @app.get('/api/v1/memory')
    async def search(conversation_id: UUID | None = None,
                     query: str | None = Query(default=None, max_length=200), context=Depends(principal)):
        return await memory.read(context, conversation_id, query)

    @app.get('/api/v1/memory/retention')
    async def retention(context=Depends(principal)):
        return await memory.retention(context)

    @app.get('/api/v1/memory/{memory_id}')
    async def get(memory_id: UUID, context=Depends(principal)):
        return await memory.read(context, memory_id=memory_id)

    @app.delete('/api/v1/memory/sources/{source_id}')
    async def erase(source_id: UUID, context=Depends(mutation)):
        return await memory.erase(context, source_id)
