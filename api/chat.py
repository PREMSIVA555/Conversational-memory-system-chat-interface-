"""`POST /chat` -- the endpoint that produces a reply and then remembers it.

M2 built the async-capture ordering guarantee; M5 (plan steps 11-12) put memory
on the way *in* and replaced the fake chunking with real provider token
streaming. Both properties hold simultaneously and neither may be traded for the
other.

THE M2 ORDERING GUARANTEE (unchanged)
-------------------------------------
**Capture is enqueued only after the response body has finished streaming**, and
enqueuing is a non-blocking `put_nowait`. There is no path by which extraction,
PII scanning, embedding, dedup or a database write can delay a single byte of
the user's reply.

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

WHAT M5 CHANGED
---------------
The reply now comes from `graphs/response_graph.py` rather than a bare
`llm.config.complete()`, so retrieved memory reaches the prompt. The graph emits
a metadata event the instant retrieval and composition finish -- before any
token exists -- and this module pulls that event off the stream **before**
constructing the response, which is what lets `degraded` and `memory_ids` ride
out as response headers (plan step 12). Headers are written before the first
body byte by definition, so "retrieval completes before the first token" is
enforced by the transport itself, not merely by convention.

Body bytes are still plain UTF-8 text, deliberately: `frontend/app/api/chat/route.ts`
pipes the body straight through, and switching to SSE would have broken it for
no gain -- the metadata has a header to travel in.

WHY THE STREAM IS DRAINED ONE EVENT BEFORE THE RESPONSE IS BUILT
----------------------------------------------------------------
`await anext(events)` runs the whole retrieval node eagerly, inside the request
handler, where an exception becomes an ordinary 500 with a traceback. If instead
the generator were handed to `StreamingResponse` untouched, a retrieval failure
would surface *after* the 200 and the response headers had already gone out, and
the client would see a truncated body with no way to tell it from a short answer.
"""

from __future__ import annotations

import uuid
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

# SYSTEM_PROMPT is re-exported rather than redefined: the prompt now lives with
# the graph that assembles it, so there is one definition instead of two that can
# drift apart. Importers of `api.chat.SYSTEM_PROMPT` are unaffected.
from graphs.response_graph import SYSTEM_PROMPT, run_response, stream_response

from capture.metrics import log_event
from capture.worker import get_worker

router = APIRouter(tags=["chat"])

__all__ = ["router", "chat", "ChatRequest", "SYSTEM_PROMPT"]


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


def _metadata_headers(event: dict[str, Any]) -> dict[str, str]:
    """Turn the graph's leading metadata event into response headers (step 12).

    `X-Memory-Degraded` and `X-Memory-Ids` carry what context the answer was
    built from, for M6's stream reader and M7's audit log to consume. Ids are a
    comma-joined list of uuids -- ASCII by construction, so no header-encoding
    surprise -- and `X-Memory-Count` is sent separately so a consumer can
    distinguish "no memories" from "the header was truncated by a proxy" without
    parsing.

    NOT YET REACHING THE BROWSER: `frontend/app/api/chat/route.ts` forwards only
    `X-Subject-Id` and `X-Actor-Id` from the upstream response, so these four are
    dropped by that proxy today. Adding them to its forward list is M6's change,
    not this module's.

    `X-Memory-Degraded: false` with an empty id list means the memory layer
    worked and found nothing; `true` means it was skipped. See
    `graphs/response_state.py` on why conflating those two is not acceptable.
    """
    memory_ids = [str(i) for i in (event.get("memory_ids") or [])]
    headers = {
        "X-Memory-Degraded": "true" if event.get("degraded") else "false",
        "X-Memory-Count": str(len(memory_ids)),
        "X-Memory-Ids": ",".join(memory_ids),
    }
    reason = event.get("degraded_reason")
    if reason:
        headers["X-Memory-Degraded-Reason"] = str(reason)
    # Proxies strip unknown headers from a cross-origin response unless they are
    # named here; without it the browser would receive them and hide them from
    # the page's JavaScript.
    headers["Access-Control-Expose-Headers"] = (
        "X-Subject-Id, X-Actor-Id, X-Memory-Degraded, X-Memory-Count, "
        "X-Memory-Ids, X-Memory-Degraded-Reason"
    )
    return headers


@router.post("/chat")
async def chat(request: ChatRequest):
    """Answer the user with memory in context, then queue the turn for capture."""
    subject_id, actor_id = _identity(request)
    headers = {"X-Subject-Id": subject_id, "X-Actor-Id": actor_id}
    messages = [{"role": "user", "content": request.message}]

    if not request.stream:
        state = await run_response(subject_id, actor_id, messages)
        reply = state.get("reply") or ""
        memory_ids = [str(i) for i in (state.get("memory_ids") or [])]
        headers.update(
            _metadata_headers(
                {
                    "degraded": state.get("degraded"),
                    "memory_ids": memory_ids,
                    "degraded_reason": state.get("degraded_reason"),
                }
            )
        )
        background = (
            BackgroundTask(
                _enqueue_capture_background, subject_id, actor_id, request.message, reply
            )
            if request.capture
            else None
        )
        return JSONResponse(
            {
                "reply": reply,
                "subject_id": subject_id,
                "actor_id": actor_id,
                # Plan step 12, on the non-streamed shape too: the same two
                # facts, so a client never has to read headers to get them.
                "degraded": bool(state.get("degraded")),
                "memory_ids": memory_ids,
            },
            headers=headers,
            background=background,
        )

    events = stream_response(subject_id, actor_id, messages)

    # Retrieval + composition run here, before a single response byte is
    # committed -- see the module docstring on why this is drained eagerly.
    metadata = await events.__anext__()
    headers.update(_metadata_headers(metadata))

    async def body() -> AsyncIterator[bytes]:
        parts: list[str] = []
        try:
            async for event in events:
                if event.get("type") != "token":
                    continue
                text = event.get("text") or ""
                parts.append(text)
                yield text.encode("utf-8")
        finally:
            # Close the graph generator on every exit path, including the
            # `GeneratorExit` Starlette throws in when a client disconnects
            # mid-stream. Without it the LangGraph task keeps pulling provider
            # tokens into a queue nobody will ever read, holding an upstream
            # socket open for the rest of the completion.
            await events.aclose()

        # The response body is complete -- reached only on a full, uninterrupted
        # stream, so a half-received turn is never captured as if it were the
        # whole reply. Only now is capture queued, and the call below does not
        # await anything.
        if request.capture:
            _enqueue_capture(subject_id, actor_id, request.message, "".join(parts))

    return StreamingResponse(body(), media_type="text/plain; charset=utf-8", headers=headers)
