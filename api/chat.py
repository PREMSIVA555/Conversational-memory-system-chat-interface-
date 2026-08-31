"""`POST /chat` -- the endpoint that produces a reply and then remembers it (plan step 12).

The ordering constraint is the whole point of this module: **capture is enqueued
only after the response body has finished streaming**, and enqueuing is a
non-blocking `put_nowait`. There is no path by which extraction, PII scanning,
embedding, dedup or a database write can delay a single byte of the user's reply.

Two response shapes, both preserving that ordering:

``stream=true`` (default)
    A `StreamingResponse` over an async generator. The generator yields every
    chunk of the reply, and only then -- after the final yield, as Starlette
    pulls the generator to exhaustion -- calls `enqueue()`. Code after the last
    `yield` runs while the response is being finalised, which is exactly "after
    the response has finished streaming".

``stream=false``
    A JSON body with a Starlette `BackgroundTask`. Starlette runs background
    tasks *after* the response has been sent, so the ordering guarantee holds
    identically.

M2 chunks an already-complete completion rather than streaming provider tokens;
true token streaming is M5's response graph. The async-capture seam this
milestone is judged on is unaffected either way.
"""

from __future__ import annotations

import uuid
from typing import AsyncIterator, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from llm import config as llm_config

from capture.metrics import log_event
from capture.worker import get_worker

router = APIRouter(tags=["chat"])

SYSTEM_PROMPT = (
    "You are a helpful personal assistant with long-term memory. "
    "Answer the user directly and concisely."
)

#: Characters per streamed chunk. Small enough that a client observes several
#: distinct chunks, large enough not to flood the transport.
CHUNK_SIZE = 24


class ChatRequest(BaseModel):
    message: str = Field(..., description="The user's turn.")
    subject_id: Optional[str] = Field(
        None, description="Whose memory this turn belongs to. Generated when omitted."
    )
    actor_id: Optional[str] = Field(
        None, description="Who is writing. Defaults to subject_id (single-user mode)."
    )
    stream: bool = Field(True, description="Stream the reply body; capture is async either way.")
    capture: bool = Field(True, description="Set false to reply without remembering the turn.")


def _identity(request: ChatRequest) -> tuple[str, str]:
    subject_id = request.subject_id or str(uuid.uuid4())
    actor_id = request.actor_id or subject_id
    return subject_id, actor_id


def _chunks(text: str, size: int = CHUNK_SIZE) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _enqueue_capture(subject_id: str, actor_id: str, message: str, reply: str) -> None:
    """Hand the finished turn to the background worker. Non-blocking by construction."""
    job_id = get_worker().enqueue(
        subject_id, actor_id, {"user": message, "assistant": reply}
    )
    log_event(
        "chat.capture.enqueued",
        job_id=job_id,
        subject_id=subject_id,
        reply_chars=len(reply),
    )


async def _enqueue_capture_background(
    subject_id: str, actor_id: str, message: str, reply: str
) -> None:
    """Async wrapper used for the non-streaming path's ``BackgroundTask``.

    WHY THIS EXISTS — it fixes a 500 that only the non-streaming path hit.

    ``_enqueue_capture`` is deliberately a plain ``def`` so no caller can await
    it, which is exactly right for the streaming path where it is called inside
    the response generator with a loop already running.

    But Starlette's ``BackgroundTask`` inspects the callable: a **sync** function
    is dispatched via ``run_in_threadpool``, and in that worker thread there is
    no running event loop, so ``get_worker()`` — which builds an
    ``asyncio.Queue`` — raises ``RuntimeError: no running event loop`` and the
    request 500s *after* the reply was generated.

    Wrapping it in an ``async def`` makes Starlette await it on the loop
    instead. The wrapper adds no await of its own, so the non-blocking property
    the streaming path relies on is preserved.

    Found in production, not by the suite: every test and the default request
    shape use ``stream=True``, so the threadpool branch was never exercised.
    """
    _enqueue_capture(subject_id, actor_id, message, reply)


async def _generate_reply(message: str) -> str:
    return await llm_config.complete(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ]
    )


@router.post("/chat")
async def chat(request: ChatRequest):
    """Answer the user, then (and only then) queue the turn for capture."""
    subject_id, actor_id = _identity(request)
    headers = {"X-Subject-Id": subject_id, "X-Actor-Id": actor_id}

    reply = await _generate_reply(request.message)

    if not request.stream:
        background = (
            BackgroundTask(
                _enqueue_capture_background, subject_id, actor_id, request.message, reply
            )
            if request.capture
            else None
        )
        return JSONResponse(
            {"reply": reply, "subject_id": subject_id, "actor_id": actor_id},
            headers=headers,
            background=background,
        )

    async def body() -> AsyncIterator[bytes]:
        for chunk in _chunks(reply):
            yield chunk.encode("utf-8")
        # The response body is complete. Only now is capture queued, and the
        # call below does not await anything.
        if request.capture:
            _enqueue_capture(subject_id, actor_id, request.message, reply)

    return StreamingResponse(body(), media_type="text/plain; charset=utf-8", headers=headers)
