"""M5 integration: the response graph on the live chat endpoint.

Two tests, both about the *ordering and shape* of the stream rather than about
the breaker (which `tests/reliability/` owns):

  `test_response_streams_token_chunks`      more than one chunk, arriving over
                                            time, not one buffered body
  `test_retrieval_completes_before_first_token`
                                            memory is fully composed before any
                                            token is emitted

WHY THESE DRIVE THE ASGI APP DIRECTLY INSTEAD OF USING `httpx`
---------------------------------------------------------------
`httpx.ASGITransport` cannot answer either question, and would have made both
tests lie. It runs the app to completion, collects every `http.response.body`
message into a list, and then hands the response a stream whose entire
implementation is:

    class ASGIResponseStream(AsyncByteStream):        # httpx/_transports/asgi.py
        async def __aiter__(self):
            yield b"".join(self._body)

So a perfectly-streaming endpoint is indistinguishable from a fully-buffered one
through that transport: `aiter_text()` yields exactly one chunk either way, and
every chunk carries the same arrival timestamp. The first draft of this file used
it and `test_response_streams_token_chunks` failed against an endpoint that was
in fact streaming 30+ provider chunks.

`drive_chat()` below therefore calls `app(scope, receive, send)` itself and
records each ASGI `http.response.body` message with the instant it was sent. That
is what the server actually emits, one layer *below* where any client-side
buffering could hide it -- strictly better evidence than a socket would give,
since an OS buffer can coalesce writes too. It is still fully in-process: no dev
server, which serves whatever was on disk when it started and has already caused
three false verifications in this project.

WHY RETRIEVAL IS STUBBED AND THE LLM IS NOT (in the first test)
----------------------------------------------------------------
Retrieval is stubbed because the semantic path embeds, and the Voyage key allows
three requests a minute; a test that waited out a backoff window would be slow
and would fail under any parallelism. The completion, though, is a **real Groq
streaming call** in `test_response_streams_token_chunks` -- the claim being
verified is "the endpoint streams provider tokens", and stubbing the provider
would verify only that the stub was iterated.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from retrieve.types import HybridResult, RetrievalCandidate, SEMANTIC

pytestmark = pytest.mark.timeout(180)

MEMORY_CONTENT = "The user's cat is called Biscuit."


# ---------------------------------------------------------------------------
# a minimal ASGI driver that preserves chunk boundaries and timing
# ---------------------------------------------------------------------------

@dataclass
class ChatCapture:
    """Everything the server emitted, in order, with timestamps."""

    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    chunks: list[bytes] = field(default_factory=list)
    arrivals: list[float] = field(default_factory=list)
    start_at: float = 0.0
    headers_at: float = 0.0

    @property
    def body(self) -> str:
        return b"".join(self.chunks).decode("utf-8")

    @property
    def first_byte_at(self) -> float:
        return self.arrivals[0]

    @property
    def span_ms(self) -> float:
        return (self.arrivals[-1] - self.arrivals[0]) * 1000 if len(self.arrivals) > 1 else 0.0


async def drive_chat(app: Any, payload: dict[str, Any]) -> ChatCapture:
    """POST `payload` to `/chat` through the raw ASGI interface.

    Empty body messages are ignored: Starlette sends a final zero-length
    `http.response.body` to close the response, and counting it would inflate
    the chunk count by one on every run -- including for a fully-buffered
    endpoint, which would make the `> 1` assertion pass for the wrong reason.
    """
    raw = json.dumps(payload).encode("utf-8")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/chat",
        "raw_path": b"/chat",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"m5.test"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode()),
        ],
        "client": ("127.0.0.1", 51234),
        "server": ("m5.test", 80),
    }

    capture = ChatCapture(start_at=time.perf_counter())
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        # The client stays connected but has nothing more to say. Returning a
        # disconnect here would let Starlette abort the response mid-stream.
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            capture.status = message["status"]
            capture.headers = {
                k.decode().lower(): v.decode() for k, v in message.get("headers", [])
            }
            capture.headers_at = time.perf_counter()
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                capture.chunks.append(body)
                capture.arrivals.append(time.perf_counter())

    await app(scope, receive, send)
    return capture


# ---------------------------------------------------------------------------
# local fixtures
# ---------------------------------------------------------------------------
#
# `tests/integration/conftest.py` belongs to M2 and is not M5's to edit, so the
# few seams these two tests need are defined here. `tests/conftest.py`'s autouse
# pool/worker teardown and the repo-root LiteLLM cache flush are inherited
# automatically -- do not re-add either.

def _candidate(content: str, score: float) -> RetrievalCandidate:
    return RetrievalCandidate(
        memory_id=str(uuid.uuid4()),
        content=content,
        score=score,
        path=SEMANTIC,
        paths={SEMANTIC},
        path_scores={SEMANTIC: score},
        raw_path_scores={SEMANTIC: score},
        metadata={"importance": 0.9, "reinforcement_count": 3},
    )


@pytest.fixture
def stubbed_retrieval(monkeypatch: pytest.MonkeyPatch):
    """Replace `hybrid_search` with an in-memory result. No embedding call.

    Records the instant it returned, so a test can compare it with the instant
    the first token was sent.
    """
    import retrieve.hybrid as hybrid_module

    state: dict[str, Any] = {
        "calls": 0,
        "returned_at": None,
        "candidates": [_candidate(MEMORY_CONTENT, 0.93)],
    }

    async def fake_hybrid_search(query):
        state["calls"] += 1
        # A small but real delay, so "retrieval finished before the first token"
        # is a measurable claim rather than two timestamps in the same
        # microsecond that would compare equal whatever the ordering.
        await asyncio.sleep(0.05)
        state["returned_at"] = time.perf_counter()
        return HybridResult(
            candidates=list(state["candidates"]),
            degraded={},
            path_counts={SEMANTIC: len(state["candidates"]), "keyword": 0},
            elapsed_ms=50.0,
        )

    monkeypatch.setattr(hybrid_module, "hybrid_search", fake_hybrid_search)
    return state


@pytest.fixture
async def closed_circuit(monkeypatch: pytest.MonkeyPatch):
    """A breaker on a test-private key, reset so the circuit starts closed.

    Namespaced away from the production `memsys:breaker:retrieval` key so these
    tests neither read nor clobber the state a running dev server is using.
    """
    import retrieve.breaker as breaker_module

    instance = breaker_module.CircuitBreaker(key=f"memsys:test:m5int:{uuid.uuid4().hex}")
    await instance.reset()
    monkeypatch.setattr(breaker_module, "get_breaker", lambda: instance)
    try:
        yield instance
    finally:
        await instance.reset()
        await instance.aclose()


@pytest.fixture(autouse=True)
async def _release_default_breaker():
    """Close the module-level breaker's loop-bound Redis client after each test."""
    yield
    try:
        from retrieve.breaker import reset_breaker

        await reset_breaker()
    except Exception:  # pragma: no cover
        pass


@pytest.fixture
def app():
    from api.main import app as fastapi_app

    return fastapi_app


# ---------------------------------------------------------------------------
# test_response_streams_token_chunks
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_response_streams_token_chunks(app, stubbed_retrieval, closed_circuit):
    """The endpoint emits several body messages over time, not one buffered body.

    Uses the **real** streaming completion. Two separate assertions, because
    each alone is weak:

      * more than one chunk -- a buffered body arrives as exactly one;
      * the chunks are spread over time -- a server that generated the whole
        reply and *then* sliced it would also produce many chunks, and only the
        arrival timestamps tell the two apart.
    """
    capture = await drive_chat(
        app,
        {
            "message": "In one short paragraph, describe what a circuit breaker does.",
            "subject_id": str(uuid.uuid4()),
            "stream": True,
            "capture": False,
        },
    )

    assert capture.status == 200
    assert capture.body.strip(), "the endpoint streamed an empty reply"
    assert len(capture.chunks) > 1, (
        f"expected several chunks, got {len(capture.chunks)}: {capture.body[:120]!r}"
    )
    assert capture.span_ms > 0.0, (
        "every chunk was emitted at the same instant -- this is a sliced body, "
        "not a stream"
    )

    print(
        f"\nstreamed {len(capture.chunks)} chunks, {len(capture.body)} chars; "
        f"first byte {(capture.first_byte_at - capture.start_at) * 1000:.0f}ms in, "
        f"chunks spanned {capture.span_ms:.0f}ms"
    )


# ---------------------------------------------------------------------------
# test_retrieval_completes_before_first_token
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_retrieval_completes_before_first_token(
    app, stubbed_retrieval, closed_circuit, monkeypatch
):
    """Memory is fully composed before a single token is emitted (plan step 11).

    Four independent pieces of evidence, because any one of them could be
    satisfied by an implementation that got the ordering wrong:

      1. `http.response.start` -- the headers -- was sent after retrieval
         returned, and HTTP guarantees it precedes every body byte;
      2. `memory_ids` in that header is non-empty, so *composition*, not merely
         the retrieval call, had finished before the headers went out;
      3. the stub's return timestamp precedes the first body message's;
      4. the composed block is present in the system message the model was
         handed, so the block was not merely computed but actually used.
    """
    import graphs.response_graph as response_graph

    prompts: list[list[dict[str, str]]] = []

    async def slow_tokens(prompt):
        prompts.append([dict(m) for m in prompt])
        for token in ["A ", "reply ", "with ", "memory ", "in ", "context."]:
            await asyncio.sleep(0.01)
            yield token

    monkeypatch.setattr(response_graph, "stream_tokens", slow_tokens)

    capture = await drive_chat(
        app,
        {
            "message": "What is my cat called?",
            "subject_id": str(uuid.uuid4()),
            "stream": True,
            "capture": False,
        },
    )

    assert capture.status == 200
    assert capture.chunks, "no tokens were streamed"
    assert stubbed_retrieval["calls"] == 1
    assert stubbed_retrieval["returned_at"] is not None

    # (1) headers were sent after retrieval returned
    assert capture.headers_at > stubbed_retrieval["returned_at"], (
        "response headers were committed before retrieval returned"
    )

    # (2) composition had finished by then
    assert capture.headers["x-memory-degraded"] == "false"
    memory_ids = [i for i in capture.headers["x-memory-ids"].split(",") if i]
    assert memory_ids, "no memory ids on the response: composition did not complete"
    assert capture.headers["x-memory-count"] == str(len(memory_ids))

    # (3) and the first body byte came later still
    assert stubbed_retrieval["returned_at"] < capture.first_byte_at, (
        "the first token was emitted before retrieval returned"
    )

    # (4) the composed block genuinely reached the model
    assert prompts, "the response node never ran"
    system_message = prompts[-1][0]
    assert system_message["role"] == "system"
    assert MEMORY_CONTENT in system_message["content"], (
        "the composed memory block never reached the prompt"
    )

    print(
        f"\nretrieval returned {(stubbed_retrieval['returned_at'] - capture.start_at) * 1000:.0f}ms in, "
        f"headers at {(capture.headers_at - capture.start_at) * 1000:.0f}ms, "
        f"first token at {(capture.first_byte_at - capture.start_at) * 1000:.0f}ms"
    )
