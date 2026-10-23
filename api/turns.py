"""Owner-authorized status is recovery authority; SSE replays persisted event IDs."""
import asyncio
import json
import re
from fastapi import Depends,HTTPException,Request
from fastapi.responses import StreamingResponse
from store.banking_turns import Turns,TurnRequest

def install(app,database,principal,mutation,*,enabled=True,run_seconds=10):
    turns=Turns(database,run_seconds=run_seconds);app.state.turns=turns
    app.state.turn_acceptance_enabled=enabled
    base='/api/v1/conversations/{conversation_id}/turns'
    @app.post(base,status_code=202)
    async def accept(conversation_id:str,body:TurnRequest,context=Depends(mutation)):
        if not app.state.turn_acceptance_enabled:raise HTTPException(503)
        return await turns.accept(context,conversation_id,body)
    @app.get(base)
    async def listing(conversation_id:str,context=Depends(principal)):
        return await turns.list(context,conversation_id)
    @app.get(base+'/by-client/{client_turn_id}')
    async def original(conversation_id:str,client_turn_id:str,context=Depends(principal)):
        return await turns.get(context,conversation_id,client_id=client_turn_id)
    @app.get(base+'/{turn_id}')
    async def status(conversation_id:str,turn_id:str,context=Depends(principal)):
        return await turns.get(context,conversation_id,turn_id=turn_id)
    @app.get(base+'/{turn_id}/events')
    async def events(conversation_id:str,turn_id:str,request:Request,context=Depends(principal)):
        state=await turns.get(context,conversation_id,turn_id=turn_id)
        value=request.headers.get('last-event-id',request.query_params.get('after_event_id',request.query_params.get('last_event_id','0')))
        if not re.fullmatch(r'0|[1-9][0-9]{0,12}',value):raise HTTPException(422)
        cursor=int(value)
        if cursor>state['last_event_id']:raise HTTPException(422)
        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                try:
                    await database.sessions.revalidate(context)
                    rows=await turns.events(context,turn_id,cursor)
                    for row in rows:
                        await database.sessions.revalidate(context)
                        await turns.get(context,conversation_id,turn_id=turn_id)
                        cursor=row['event_id']
                        data=dict(turn_id=turn_id,**row['payload'])
                        yield f'id: {cursor}\nevent: {row["event_type"]}\ndata: {json.dumps(data,ensure_ascii=False,default=str)}\n\n'
                    current=await turns.get(context,conversation_id,turn_id=turn_id)
                    if current['result'] is not None and cursor>=current['last_event_id']:return
                except HTTPException:return
                yield ': heartbeat\n\n'
                await asyncio.sleep(.15)
        return StreamingResponse(stream(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})
