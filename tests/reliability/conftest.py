"""Fixtures for the M5 reliability suite (plan step 14).

Four things live here, and each exists because a naive version of the
corresponding test would pass while proving nothing:

`redis_key` / `breaker`
    A **real** Redis-backed breaker on a per-test namespaced key, flushed before
    and after. Not a fake: `test_breaker_open_state_visible_to_second_replica`
    is precisely a test that the state is not in Python memory, so a mock would
    invert the thing under test.

`second_replica`
    Builds a *separately constructed* `CircuitBreaker` on the same key with its
    own Redis client. Two objects sharing nothing but Redis is what "another
    pod" means here.

`retrieval_stub`
    A controllable `hybrid_search` replacement: succeed, raise, or hang. It
    never touches Postgres or Voyage. That is not just speed — the Voyage key
    allows 3 requests/minute, so a reliability suite that embedded for real
    would spend most of its runtime in a backoff sleep and would fail under any
    parallelism. These tests are about the breaker, not the retriever.

`fake_clock`
    The injected `now()` (plan step 15). Cooldown expiry is tested by assignment,
    not by `asyncio.sleep(30)`.

WHAT IS DELIBERATELY *NOT* HERE
-------------------------------
No copy of `_isolate_loop_bound_resources` and no LiteLLM cache flush. Both are
autouse fixtures inherited from `tests/conftest.py` and the repo-root
`conftest.py` respectively; a second copy would run their teardown twice per
test. See the notes in `tests/integration/conftest.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Optional

import httpx
import pytest

from retrieve.breaker import CircuitBreaker
from retrieve.types import HybridResult, RetrievalCandidate, SEMANTIC
from store.db import ensure_selector_event_loop_policy, load_env, redis_url

ensure_selector_event_loop_policy()
load_env()

#: Every test in this suite is bounded. A breaker bug's most likely symptom is a
#: hang -- a probe lock never released, a queue never drained -- and an unbounded
#: hang in CI is indistinguishable from a slow machine.
pytestmark = pytest.mark.timeout(60)


# ---------------------------------------------------------------------------
# clock injection (plan step 15)
# ---------------------------------------------------------------------------

class FakeClock:
    """A monotonic clock the test advances by hand.

    Starts at a fixed, plausible epoch rather than 0: the breaker stores
    `opened_at` as an absolute timestamp, and a zero epoch would make an
    "elapsed" computation of `now - opened_at` accidentally correct even if the
    code had ignored `opened_at` entirely.
    """

    def __init__(self, start: float = 1_756_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> float:
        self.value += seconds
        return self.value


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


# ---------------------------------------------------------------------------
# redis-backed breakers
# ---------------------------------------------------------------------------

@pytest.fixture
def redis_key() -> str:
    """A namespaced key unique to this test.

    Per-test rather than a single shared constant so tests cannot leak state
    into each other, and so the suite is safe to run while a dev server is up
    against the same Redis using the real `memsys:breaker:retrieval` key.
    """
    return f"memsys:test:breaker:{uuid.uuid4().hex}"


def make_breaker(key: str, clock: Any = None, **kw: Any) -> CircuitBreaker:
    """Construct a breaker the way a fresh replica would.

    Defaults are tightened for tests: threshold 3 (the production default,
    spelled out so a test reads without a lookup), a 30s cooldown, and a short
    connect timeout so the Redis-down test fails fast instead of sitting out a
    default socket timeout.
    """
    return CircuitBreaker(
        key=key,
        clock=clock,
        failure_threshold=kw.pop("failure_threshold", 3),
        cooldown_seconds=kw.pop("cooldown_seconds", 30.0),
        connect_timeout=kw.pop("connect_timeout", 2.0),
        **kw,
    )


@pytest.fixture
async def breaker(redis_key: str, fake_clock: FakeClock):
    """Replica A: the instance under test. Cleaned up at both ends."""
    instance = make_breaker(redis_key, fake_clock)
    await instance.reset()
    try:
        yield instance
    finally:
        await instance.reset()
        await instance.aclose()


@pytest.fixture
async def second_replica(redis_key: str, fake_clock: FakeClock):
    """Replica B: a *separately constructed* breaker on the same Redis key.

    It shares the key and the clock and nothing else -- its own object, its own
    connection, its own script cache. Anything it observes about the circuit, it
    observed through Redis.
    """
    instances: list[CircuitBreaker] = []

    def build(**kw: Any) -> CircuitBreaker:
        instance = make_breaker(redis_key, kw.pop("clock", fake_clock), **kw)
        instances.append(instance)
        return instance

    try:
        yield build
    finally:
        for instance in instances:
            await instance.aclose()


# ---------------------------------------------------------------------------
# the controllable retrieval stub
# ---------------------------------------------------------------------------

def make_candidate(memory_id: str, content: str, score: float = 0.9) -> RetrievalCandidate:
    """A candidate shaped exactly like one `hybrid_search` would return."""
    return RetrievalCandidate(
        memory_id=memory_id,
        content=content,
        score=score,
        path=SEMANTIC,
        paths={SEMANTIC},
        path_scores={SEMANTIC: score},
        raw_path_scores={SEMANTIC: score},
        metadata={"importance": 0.8, "reinforcement_count": 2},
    )


class RetrievalStub:
    """A `hybrid_search` stand-in whose failure mode the test sets.

    Modes:
      ``ok``     return `candidates` immediately
      ``raise``  raise `error` (a dependency being down)
      ``hang``   sleep `hang_for` seconds, so the guard's `wait_for` fires

    `calls` counts invocations, which is how `test_concurrent_half_open_probes_single_flight`
    proves only one probe actually executed -- the breaker's own state cannot
    show that, since both replicas end up observing the same final state either
    way.
    """

    def __init__(self) -> None:
        self.mode = "ok"
        self.calls = 0
        self.queries: list[Any] = []
        self.error: BaseException = RuntimeError("simulated retrieval failure")
        self.hang_for = 30.0
        self.candidates: list[RetrievalCandidate] = [
            make_candidate(str(uuid.uuid4()), "The user's cat is called Biscuit.", 0.91),
            make_candidate(str(uuid.uuid4()), "The user is allergic to shellfish.", 0.72),
        ]
        self.started = asyncio.Event()

    async def __call__(self, query: Any) -> HybridResult:
        self.calls += 1
        self.queries.append(query)
        self.started.set()
        if self.mode == "raise":
            raise self.error
        if self.mode == "hang":
            await asyncio.sleep(self.hang_for)
        return HybridResult(
            candidates=list(self.candidates),
            degraded={},
            path_counts={SEMANTIC: len(self.candidates), "keyword": 0},
            elapsed_ms=1.0,
        )

    # -- readable mode setters --------------------------------------------

    def succeed(self) -> "RetrievalStub":
        self.mode = "ok"
        return self

    def fail(self, error: BaseException | None = None) -> "RetrievalStub":
        self.mode = "raise"
        if error is not None:
            self.error = error
        return self

    def hang(self, seconds: float = 30.0) -> "RetrievalStub":
        self.mode = "hang"
        self.hang_for = seconds
        return self


@pytest.fixture
def retrieval_stub() -> RetrievalStub:
    return RetrievalStub()


@pytest.fixture
def patched_retrieval(monkeypatch: pytest.MonkeyPatch, retrieval_stub: RetrievalStub):
    """Point `retrieve.guarded`'s late-bound lookup at the stub.

    Patches `retrieve.hybrid.hybrid_search` -- the module attribute
    `guarded_hybrid_search` resolves at call time -- so the guard, the breaker
    and the graph all run for real and only the network-touching leaf is
    replaced.
    """
    import retrieve.hybrid as hybrid_module

    monkeypatch.setattr(hybrid_module, "hybrid_search", retrieval_stub)
    return retrieval_stub


# ---------------------------------------------------------------------------
# the LLM stub
# ---------------------------------------------------------------------------

class TokenStub:
    """A streaming-completion stand-in. Records the prompt it was handed.

    Recording is what lets `test_chat_returns_200_with_reply_while_circuit_open`
    assert "no memory context in the prompt" against the actual prompt rather
    than against a proxy like an empty `memory_ids` header, which a buggy graph
    could report while still injecting the block.
    """

    def __init__(self, tokens: Optional[list[str]] = None) -> None:
        self.tokens = tokens or ["Hello", ", ", "there", ". ", "How ", "can ", "I ", "help?"]
        self.prompts: list[list[dict[str, str]]] = []

    def __call__(self, prompt: list[dict[str, str]]):
        self.prompts.append([dict(m) for m in prompt])

        async def generate():
            for token in self.tokens:
                yield token

        return generate()

    @property
    def last_prompt_text(self) -> str:
        if not self.prompts:
            return ""
        return "\n".join(m.get("content", "") for m in self.prompts[-1])


@pytest.fixture
def token_stub(monkeypatch: pytest.MonkeyPatch) -> TokenStub:
    """Replace the graph's streaming LLM call. No provider round-trip."""
    import graphs.response_graph as response_graph

    stub = TokenStub()
    monkeypatch.setattr(response_graph, "stream_tokens", stub)
    return stub


# ---------------------------------------------------------------------------
# the app under test
# ---------------------------------------------------------------------------

@pytest.fixture
async def patched_default_breaker(monkeypatch: pytest.MonkeyPatch, redis_key: str, fake_clock: FakeClock):
    """Make `retrieve.guarded`'s default breaker this test's namespaced one.

    The endpoint does not take a breaker argument -- it uses the process default
    -- so an endpoint-level test has to redirect that default rather than pass
    an instance in.
    """
    import retrieve.breaker as breaker_module

    instance = make_breaker(redis_key, fake_clock)
    await instance.reset()
    monkeypatch.setattr(breaker_module, "get_breaker", lambda: instance)
    try:
        yield instance
    finally:
        await instance.reset()
        await instance.aclose()


@pytest.fixture
async def api_client():
    """An httpx client wired straight to the ASGI app -- no network, no server.

    In-process on purpose: the long-running dev server serves whatever was on
    disk when it started, and a reliability suite that talked to it would be
    testing a stale build.
    """
    from api.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://reliability.test", timeout=30.0
    ) as client:
        yield client


@pytest.fixture(autouse=True)
async def _release_default_breaker():
    """Drop the module-level breaker's Redis client after every test.

    Same class of problem as the connection pools `tests/conftest.py` handles: a
    redis client binds to the loop it was built on, and pytest-asyncio closes
    that loop between tests.
    """
    yield
    try:
        from retrieve.breaker import reset_breaker

        await reset_breaker()
    except Exception:  # pragma: no cover - teardown must never mask a failure
        pass


def redis_dsn() -> str:
    """The live Redis URL, read from `infra/.env`. Never hardcode the port."""
    return redis_url()
