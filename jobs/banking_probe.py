"""P01 isolated worker-process access/trace probe, not a durable P02 consumer.

Input is a trusted local pipe, never an HTTP/model tool surface. Even forged
envelopes must pass current session and resource ownership checks.
"""
import asyncio
from dataclasses import fields
import json
import os
import sys
from opentelemetry.trace import SpanContext,TraceFlags
from fastapi import HTTPException
from auth.config import Settings
from auth.sessions import Sessions,RequestContext
from api.privacy import Telemetry,configure_security_logging
from store.banking import Database
from jobs.banking_access import run_queued


async def probe(data):
    settings=Settings.from_env(load_dotenv=False)
    sessions=Sessions(settings)
    database=Database(settings,sessions)
    telemetry=Telemetry()
    configure_security_logging()
    await database.open()
    try:
        if set(data)!={'context','resource_id','trace_id','span_id'}:
            raise HTTPException(422,'Invalid operation')
        ctx=RequestContext(**data['context'])
        parent=SpanContext(int(data['trace_id'],16),int(data['span_id'],16),True,TraceFlags(1))
        if not parent.is_valid:
            raise HTTPException(422,'Invalid correlation')
        async def operation(context):
            # Use the actual constrained worker pool, not API runtime credentials.
            await database.inspect_role(database.worker)
            return await database.conversation(context,data['resource_id'],worker=True)
        await run_queued(sessions,ctx,telemetry,parent,operation)
        telemetry.provider.force_flush()
        return dict(status=200,found=True,process_id=os.getpid(),spans=list(telemetry.exporter.records))
    except HTTPException as exc:
        return dict(status=exc.status_code,found=False)
    except Exception:
        return dict(status=503,found=False)
    finally:
        await database.close()
        await sessions.close()
        telemetry.close()


if __name__=='__main__':
    try:
        request=json.loads(sys.stdin.read(8192))
        response=asyncio.run(probe(request))
    except Exception:
        response=dict(status=503,found=False)
    print(json.dumps(response))
