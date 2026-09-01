"""M5 reliability suite: the circuit breaker and the degraded-reply fallback.

Ten tests, in the plan's order. Two properties run through all of them and are
worth naming up front, because they are what separates this suite from one that
would pass against a fake:

1. **The state is in Redis.** Every breaker here talks to the live Redis from
   `infra/.env` on a per-test namespaced key. `test_breaker_open_state_visible_to_second_replica`
   would pass trivially against a process-local counter if both "replicas" were
   the same object; it uses two separately constructed instances precisely so
   that it cannot.

2. **Nothing here embeds or queries Postgres.** Retrieval is stubbed
   (`patched_retrieval`) and the LLM is stubbed (`token_stub`). The subject is
   the breaker, and a suite that also exercised the Voyage embedding path would
   spend its time in a 3-request/minute backoff instead of testing anything.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from retrieve.breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker
from retrieve.guarded import RetrievalUnavailable, guarded_hybrid_search
from retrieve.types import RetrievalQuery

# NOTE: no `from .conftest import ...`. `tests/` is not a package (there is no
# `__init__.py` anywhere under it), so a relative import would fail at collection
# time. Anything shared with `conftest.py` travels as a fixture, which is how
# pytest intends it.

pytestmark = pytest.mark.timeout(60)

THRESHOLD = 3
COOLDOWN = 30.0


def a_query(text: str = "what is my cat called?") -> RetrievalQuery:
    subject = str(uuid.uuid4())
    return RetrievalQuery(text=text, subject_id=subject, actor_id=subject)


async def drive_failures(breaker: CircuitBreaker, stub, count: int) -> None:
    """Run `count` guarded retrievals that fail, through the real guard.

    Deliberately not `breaker.record_failure()` in a loop: the plan's wording is
    "forces exactly N consecutive retrieval *failures*", and going through
    `guarded_hybrid_search` proves the guard actually reports failures to the
    breaker, which a direct call would assume.
    """
    stub.fail()
    for _ in range(count):
        with pytest.raises(RetrievalUnavailable):
            await guarded_hybrid_search(a_query(), breaker=breaker, search_fn=stub)


# ---------------------------------------------------------------------------
# 1. test_breaker_opens_after_n_consecutive_failures
# ---------------------------------------------------------------------------

async def test_breaker_opens_after_n_consecutive_failures(breaker, retrieval_stub):
    """Exactly N consecutive failures take the circuit from closed to open."""
    assert await breaker.state() == CLOSED

    # N-1 failures must NOT be enough -- otherwise "after N" is unproven and the
    # test would also pass for a breaker that opened on the first failure.
    await drive_failures(breaker, retrieval_stub, THRESHOLD - 1)
    interim = await breaker.snapshot()
    assert interim.state == CLOSED, f"opened early at {interim.failures} failures"
    assert interim.failures == THRESHOLD - 1

    await drive_failures(breaker, retrieval_stub, 1)

    final = await breaker.snapshot()
    assert final.state == OPEN
    assert final.failures == THRESHOLD
    assert final.opened_at == breaker.now()

    # And the open circuit now short-circuits: no further calls reach retrieval.
    calls_before = retrieval_stub.calls
    with pytest.raises(RetrievalUnavailable) as caught:
        await guarded_hybrid_search(a_query(), breaker=breaker, search_fn=retrieval_stub)
    assert caught.value.reason == "open_circuit"
    assert retrieval_stub.calls == calls_before, "an open circuit still called retrieval"


# ---------------------------------------------------------------------------
# 2. test_breaker_open_state_visible_to_second_replica
# ---------------------------------------------------------------------------

async def test_breaker_open_state_visible_to_second_replica(
    breaker, second_replica, retrieval_stub
):
    """Instance B observes the circuit A tripped -- proving state is shared.

    This is the test a process-local implementation cannot pass. B is built by a
    separate constructor call with its own Redis connection and its own script
    cache; the ONLY thing it shares with A is the key.
    """
    replica_b = second_replica()
    assert replica_b is not breaker
    assert await replica_b.state() == CLOSED

    await drive_failures(breaker, retrieval_stub, THRESHOLD)
    assert await breaker.state() == OPEN

    # B never saw a failure, never incremented a counter, and was not even
    # constructed when the circuit tripped in some orderings.
    observed = await replica_b.snapshot()
    assert observed.state == OPEN, (
        "a second replica did not observe the open circuit: the state is "
        "process-local, not shared through Redis"
    )
    assert observed.failures == THRESHOLD
    assert observed.opened_at == breaker.now()

    # B also *behaves* as open, not merely reports it.
    calls_before = retrieval_stub.calls
    with pytest.raises(RetrievalUnavailable) as caught:
        await guarded_hybrid_search(a_query(), breaker=replica_b, search_fn=retrieval_stub)
    assert caught.value.reason == "open_circuit"
    assert retrieval_stub.calls == calls_before


# ---------------------------------------------------------------------------
# 3. test_chat_returns_200_with_reply_while_circuit_open
# ---------------------------------------------------------------------------

async def test_chat_returns_200_with_reply_while_circuit_open(
    api_client, patched_default_breaker, token_stub, patched_retrieval
):
    """An open circuit degrades the reply. It never withholds one (plan step 10)."""
    await patched_default_breaker.force_open()
    assert await patched_default_breaker.state() == OPEN

    calls_before = patched_retrieval.calls

    response = await api_client.post(
        "/chat",
        json={
            "message": "What is my cat called?",
            "subject_id": str(uuid.uuid4()),
            "stream": True,
            "capture": False,
        },
    )

    assert response.status_code == 200
    assert response.text.strip(), "an open circuit produced an empty reply body"
    assert response.text == "".join(token_stub.tokens)

    # No memory context in the prompt -- asserted against the prompt the model
    # was actually handed, not against a header the graph also controls.
    assert token_stub.prompts, "the response node never ran"
    prompt_text = token_stub.last_prompt_text
    for candidate in patched_retrieval.candidates:
        assert candidate.content not in prompt_text
    assert "previously remembered" not in prompt_text

    # And retrieval was not attempted at all.
    assert patched_retrieval.calls == calls_before

    assert response.headers["x-memory-degraded"] == "true"
    assert response.headers["x-memory-ids"] == ""
    assert response.headers["x-memory-count"] == "0"


# ---------------------------------------------------------------------------
# 4. test_half_open_probe_after_cooldown_recloses_on_success
# ---------------------------------------------------------------------------

async def test_half_open_probe_after_cooldown_recloses_on_success(
    breaker, retrieval_stub, fake_clock
):
    """Cooldown elapses -> one probe is allowed -> it succeeds -> closed."""
    await drive_failures(breaker, retrieval_stub, THRESHOLD)
    assert await breaker.state() == OPEN

    # Just short of the cooldown: still open, still blocking. Without this the
    # test would pass for an implementation that ignored `opened_at` entirely.
    fake_clock.advance(COOLDOWN - 1)
    with pytest.raises(RetrievalUnavailable) as caught:
        await guarded_hybrid_search(a_query(), breaker=breaker, search_fn=retrieval_stub)
    assert caught.value.reason == "open_circuit"
    assert await breaker.state() == OPEN

    # Past the cooldown -- no real sleeping anywhere (plan step 15).
    fake_clock.advance(2)
    retrieval_stub.succeed()
    calls_before = retrieval_stub.calls

    result = await guarded_hybrid_search(a_query(), breaker=breaker, search_fn=retrieval_stub)

    assert retrieval_stub.calls == calls_before + 1, "the probe never reached retrieval"
    assert result.candidates
    recovered = await breaker.snapshot()
    assert recovered.state == CLOSED
    assert recovered.failures == 0


# ---------------------------------------------------------------------------
# 5. test_half_open_probe_failure_reopens_breaker
# ---------------------------------------------------------------------------

async def test_half_open_probe_failure_reopens_breaker(breaker, retrieval_stub, fake_clock):
    """A failed probe re-opens immediately and restarts the cooldown."""
    await drive_failures(breaker, retrieval_stub, THRESHOLD)
    first_opened_at = (await breaker.snapshot()).opened_at

    fake_clock.advance(COOLDOWN + 1)
    retrieval_stub.fail()

    with pytest.raises(RetrievalUnavailable) as caught:
        await guarded_hybrid_search(a_query(), breaker=breaker, search_fn=retrieval_stub)
    assert caught.value.reason == "error"

    reopened = await breaker.snapshot()
    assert reopened.state == OPEN
    # The cooldown restarted from *now*, not from the original trip: otherwise
    # a failed probe would be followed immediately by another one.
    assert reopened.opened_at == fake_clock.value
    assert reopened.opened_at > first_opened_at

    # Proof the restart is real: a call one second later is still blocked.
    fake_clock.advance(1)
    with pytest.raises(RetrievalUnavailable) as caught:
        await guarded_hybrid_search(a_query(), breaker=breaker, search_fn=retrieval_stub)
    assert caught.value.reason == "open_circuit"


# ---------------------------------------------------------------------------
# 6. test_retrieval_timeout_counts_as_failure
# ---------------------------------------------------------------------------

async def test_retrieval_timeout_counts_as_failure(breaker, retrieval_stub):
    """A hang past RETRIEVAL_TIMEOUT_MS is a failure, not a hang for the caller."""
    retrieval_stub.hang(30.0)

    before = (await breaker.snapshot()).failures

    with pytest.raises(RetrievalUnavailable) as caught:
        # 150ms, not the configured 6s: the point is that the *guard's* budget
        # fires, and waiting six seconds to prove it would be six wasted seconds.
        await guarded_hybrid_search(
            a_query(), breaker=breaker, search_fn=retrieval_stub, timeout_ms=150
        )

    assert caught.value.reason == "timeout"
    assert isinstance(caught.value.__cause__, asyncio.TimeoutError)

    after = await breaker.snapshot()
    assert after.failures == before + 1, "a timeout did not increment the failure counter"


# ---------------------------------------------------------------------------
# 7. test_success_resets_consecutive_failure_count
# ---------------------------------------------------------------------------

async def test_success_resets_consecutive_failure_count(breaker, retrieval_stub):
    """Failures must be *consecutive*: one success wipes the count."""
    await drive_failures(breaker, retrieval_stub, THRESHOLD - 1)
    assert (await breaker.snapshot()).failures == THRESHOLD - 1

    retrieval_stub.succeed()
    await guarded_hybrid_search(a_query(), breaker=breaker, search_fn=retrieval_stub)
    assert (await breaker.snapshot()).failures == 0

    # THRESHOLD-1 more failures. A breaker that merely accumulated would now be
    # at 2*(THRESHOLD-1) = 4 >= 3 and would have opened.
    await drive_failures(breaker, retrieval_stub, THRESHOLD - 1)

    final = await breaker.snapshot()
    assert final.state == CLOSED, "the breaker opened on non-consecutive failures"
    assert final.failures == THRESHOLD - 1


# ---------------------------------------------------------------------------
# 8. test_degraded_flag_surfaced_in_response_metadata
# ---------------------------------------------------------------------------

async def test_degraded_flag_surfaced_in_response_metadata(
    patched_default_breaker, token_stub, patched_retrieval
):
    """With the circuit open the leading metadata event says so, before any token."""
    from graphs.response_graph import stream_response

    await patched_default_breaker.force_open()

    subject = str(uuid.uuid4())
    events = [
        event
        async for event in stream_response(
            subject, subject, [{"role": "user", "content": "what do you know about me?"}]
        )
    ]

    # Plan step 10, asserted structurally rather than trusted from a docstring:
    # the conditional edge's routing table sends BOTH outcomes to the response
    # node, so no retrieval verdict can reach END without a reply.
    from graphs.response_graph import RETRIEVAL_ROUTES

    assert set(RETRIEVAL_ROUTES) == {"ok", "degraded"}
    assert set(RETRIEVAL_ROUTES.values()) == {"respond"}, (
        f"a retrieval outcome routes somewhere other than the response node: "
        f"{RETRIEVAL_ROUTES}"
    )

    assert events, "the graph produced no events at all"
    metadata = events[0]
    assert metadata["type"] == "metadata", "metadata was not the first event"
    assert metadata["degraded"] is True
    assert metadata["memory_ids"] == []
    assert metadata["memory_count"] == 0
    assert metadata["degraded_reason"] == "open_circuit"

    # Degraded still means a full reply was produced.
    tokens = [e for e in events if e["type"] == "token"]
    assert tokens
    assert "".join(e["text"] for e in tokens) == "".join(token_stub.tokens)


# ---------------------------------------------------------------------------
# 9. test_concurrent_half_open_probes_single_flight
# ---------------------------------------------------------------------------

async def test_concurrent_half_open_probes_single_flight(
    breaker, second_replica, retrieval_stub, fake_clock
):
    """Two replicas enter half_open at once; exactly one probe executes.

    Without the short-TTL probe lock (plan step 3), a cooldown expiring across a
    fleet releases a thundering herd onto the very dependency that just fell
    over. The assertion is on `stub.calls`, because the resulting *state* is the
    same either way -- only the call count distinguishes one probe from two.
    """
    replica_b = second_replica()

    await drive_failures(breaker, retrieval_stub, THRESHOLD)
    assert await breaker.state() == OPEN

    fake_clock.advance(COOLDOWN + 1)

    # A slow success, so both replicas are inside the half-open window together
    # rather than one completing before the other starts.
    retrieval_stub.hang(0.3)
    calls_before = retrieval_stub.calls

    outcomes = await asyncio.gather(
        guarded_hybrid_search(
            a_query(), breaker=breaker, search_fn=retrieval_stub, timeout_ms=10_000
        ),
        guarded_hybrid_search(
            a_query(), breaker=replica_b, search_fn=retrieval_stub, timeout_ms=10_000
        ),
        return_exceptions=True,
    )

    assert retrieval_stub.calls == calls_before + 1, (
        f"{retrieval_stub.calls - calls_before} probes executed; the half-open "
        "probe is not single-flight"
    )

    blocked = [o for o in outcomes if isinstance(o, RetrievalUnavailable)]
    succeeded = [o for o in outcomes if not isinstance(o, BaseException)]
    assert len(succeeded) == 1
    assert len(blocked) == 1
    assert blocked[0].reason == "open_circuit"
    assert blocked[0].circuit_state == HALF_OPEN

    assert await replica_b.state() == CLOSED


# ---------------------------------------------------------------------------
# 10. test_redis_unavailable_fails_open_not_closed
# ---------------------------------------------------------------------------

async def test_redis_unavailable_fails_open_not_closed(
    api_client, monkeypatch, token_stub, patched_retrieval
):
    """Redis down must degrade the BOOKKEEPING, never the reply.

    A breaker that blocks retrieval -- or worse, raises -- when its own state
    store is unreachable converts a Redis outage into a chat outage. The
    component installed to contain failures would be the one spreading them.
    So an unreachable Redis means "allowed": retrieval still has its own
    timeout, so the worst case is bounded latency, not a dead endpoint.
    """
    import retrieve.breaker as breaker_module

    # Port 1 is reserved and nothing listens there; the connection is refused
    # immediately rather than after a socket timeout.
    dead = CircuitBreaker(
        key="memsys:test:breaker:unreachable",
        url="redis://127.0.0.1:1/0",
        failure_threshold=THRESHOLD,
        cooldown_seconds=COOLDOWN,
        connect_timeout=0.25,
    )
    monkeypatch.setattr(breaker_module, "get_breaker", lambda: dead)

    # The breaker cannot even read its own state.
    assert await dead.state() == CLOSED  # unknown is reported as healthy, not open
    decision = await dead.allow()
    assert decision.allowed is True, "the breaker failed CLOSED with Redis down"
    assert decision.blind is True

    try:
        response = await api_client.post(
            "/chat",
            json={
                "message": "What is my cat called?",
                "subject_id": str(uuid.uuid4()),
                "stream": True,
                "capture": False,
            },
        )

        assert response.status_code == 200
        assert response.text.strip(), "Redis being down produced an empty reply"

        # Retrieval was still attempted and still worked, so the reply is not
        # even degraded -- only the breaker's bookkeeping was lost.
        assert patched_retrieval.calls >= 1
        assert response.headers["x-memory-degraded"] == "false"
        assert response.headers["x-memory-count"] != "0"
    finally:
        await dead.aclose()


# ---------------------------------------------------------------------------
# BEYOND THE PLAN'S TWELVE
# ---------------------------------------------------------------------------
#
# The two below are not in the M5 test list. They cover a hole found while
# working through the Definition of Done's "send a real chat message with
# Postgres stopped" step, and leaving the behaviour they check untested would
# have meant shipping a breaker that stays closed through the exact outage it
# exists for. See `retrieve/guarded.py`'s "WHAT COUNTS AS A FAILURE".


async def test_all_paths_degraded_counts_as_failure(breaker, retrieval_stub):
    """A result where every path degraded is a failure, not an empty success.

    This is what a stopped Postgres actually looks like from here: both paths
    time out inside `hybrid_search`, which isolates them and returns a
    perfectly well-formed, completely empty result. It never raises. A breaker
    watching only for exceptions would sail through a total database outage and
    keep paying the full per-path timeout on every turn.
    """
    from retrieve.types import HybridResult, KEYWORD, SEMANTIC

    async def both_paths_dead(_query):
        return HybridResult(
            candidates=[],
            degraded={SEMANTIC: "timeout after 5000ms", KEYWORD: "OperationalError: refused"},
            path_counts={SEMANTIC: 0, KEYWORD: 0},
            elapsed_ms=5001.0,
        )

    for _ in range(THRESHOLD):
        with pytest.raises(RetrievalUnavailable) as caught:
            await guarded_hybrid_search(a_query(), breaker=breaker, search_fn=both_paths_dead)
        assert caught.value.reason in {"all_paths_degraded", "open_circuit"}

    final = await breaker.snapshot()
    assert final.state == OPEN, (
        "a total retrieval outage did not open the circuit: hybrid_search "
        "swallows path failures, so this is the outage the breaker most needs "
        "to notice"
    )


async def test_breaker_still_opens_after_redis_script_cache_flush(breaker, retrieval_stub):
    """A lost script cache must not silently turn the breaker into a no-op.

    Regression test for a branch that was dead from the day it was written. The
    `EVALSHA` fallback was guarded by `if "NOSCRIPT" not in str(exc).upper()`,
    but redis-py raises `NoScriptError("No matching script. Please use EVAL.")`
    -- that substring never appears. So the guard re-raised, the stale sha was
    never dropped, `EVAL` was never reached, and the error fell through to the
    fail-open handler in `allow()`.

    The result was a breaker that, after any Redis restart, never wrote to Redis
    and never opened again for the life of the process -- while logging
    "Redis unavailable" at WARNING with Redis perfectly healthy.

    `SCRIPT FLUSH` is exactly what a restart does to the script cache, and this
    warms **all three** scripts first, because the whole suite otherwise builds
    breakers with cold caches and never executes the fallback at all.
    """
    # Warm every sha the breaker uses: allow + failure + success.
    retrieval_stub.succeed()
    await guarded_hybrid_search(a_query(), breaker=breaker, search_fn=retrieval_stub)
    await drive_failures(breaker, retrieval_stub, 1)
    await breaker.reset()

    assert set(breaker._script_shas) == {"allow", "failure", "success"}, (
        f"not every script was warmed, so the flush would not exercise the "
        f"fallback for all of them: {sorted(breaker._script_shas)}"
    )

    # Every cached sha is now stale, exactly as after a Redis restart.
    await breaker._redis().script_flush()

    await drive_failures(breaker, retrieval_stub, THRESHOLD)

    final = await breaker.snapshot()
    assert final.state == OPEN, (
        "the circuit never opened after the script cache was flushed: the "
        "EVALSHA -> EVAL fallback is not firing, so every mutation is being "
        "swallowed by the fail-open path"
    )
    assert final.failures == THRESHOLD

    # And it genuinely reached Redis, rather than being reported from memory.
    raw = await breaker._redis().get(breaker.key)
    assert raw, "nothing was written to Redis at all"
    assert '"state":"open"' in raw.replace(" ", "")

    # Recovery is complete, not one-shot: the reloaded shas work from here on.
    retrieval_stub.succeed()
    await breaker.reset()
    await guarded_hybrid_search(a_query(), breaker=breaker, search_fn=retrieval_stub)
    assert await breaker.state() == CLOSED


async def test_straggler_success_does_not_reclose_an_open_circuit(
    breaker, second_replica, retrieval_stub
):
    """A slow success that lands after the circuit opened must not re-close it.

    The multi-replica race this module is built for: replica A is admitted while
    the circuit is closed and takes a few seconds; meanwhile replica B sees N
    failures and opens the circuit. A's success then arrives describing the
    dependency *as it was before the outage was detected*.

    Applied unconditionally it would slam the circuit back to closed, discarding
    the cooldown and every finding B made -- and it gets more likely the more
    replicas there are, which is precisely backwards.
    """
    replica_b = second_replica()

    # A is admitted while everything is healthy. Its call is still in flight.
    decision_a = await breaker.allow()
    assert decision_a.allowed and not decision_a.probe
    assert decision_a.state == CLOSED

    # B trips the circuit while A is still working.
    await drive_failures(replica_b, retrieval_stub, THRESHOLD)
    tripped = await replica_b.snapshot()
    assert tripped.state == OPEN

    # A finally succeeds and reports it.
    await breaker.record_success(probe=decision_a.probe)

    after = await breaker.snapshot()
    assert after.state == OPEN, (
        "a straggler success re-closed an open circuit, discarding the cooldown "
        "and the other replica's findings"
    )
    assert after.failures == THRESHOLD
    assert after.opened_at == tripped.opened_at, "the cooldown was restarted or cleared"

    # The circuit still behaves as open for everyone.
    with pytest.raises(RetrievalUnavailable) as caught:
        await guarded_hybrid_search(a_query(), breaker=replica_b, search_fn=retrieval_stub)
    assert caught.value.reason == "open_circuit"

    # And the elected probe can still close it, so the guard blocks stragglers
    # without blocking legitimate recovery.
    breaker.now.advance(COOLDOWN + 1)
    retrieval_stub.succeed()
    await guarded_hybrid_search(a_query(), breaker=breaker, search_fn=retrieval_stub)
    assert await breaker.state() == CLOSED


async def test_straggler_success_does_not_steal_the_half_open_probe_lock(
    breaker, second_replica, retrieval_stub, fake_clock
):
    """A straggler landing on `half_open` must not close it or free the probe lock.

    The sibling of the test above, covering the case that separates the shipped
    guard from a plausible weaker one. `state ~= 'open'` looks equivalent and is
    not: `half_open` satisfies it, so a straggler would close the circuit on
    stale evidence AND `DEL` the probe lock.

    That second effect is the real damage, and it is why this asserts the lock
    rather than only the state. The lock belongs to the ONE replica the fleet
    elected to test recovery; releasing it readmits a second prober against a
    dependency that just fell over, which is exactly the thundering herd plan
    step 3 exists to prevent.

    Timeline: A is admitted while closed -> B trips the circuit -> the cooldown
    elapses -> C is elected probe and holds the lock -> A's success finally
    lands.
    """
    replica_b = second_replica()
    replica_c = second_replica()

    # A is admitted while everything is healthy. Its call is still in flight.
    decision_a = await breaker.allow()
    assert decision_a.allowed and not decision_a.probe
    assert decision_a.state == CLOSED

    # B trips the circuit while A is still working.
    await drive_failures(replica_b, retrieval_stub, THRESHOLD)
    assert await replica_b.state() == OPEN

    # The cooldown elapses and C is elected as the single probe.
    fake_clock.advance(COOLDOWN + 1)
    decision_c = await replica_c.allow()
    assert decision_c.probe is True, "C was not elected as the probe"
    assert decision_c.state == HALF_OPEN

    lock_token = await replica_c._redis().get(replica_c.probe_key)
    assert lock_token, "the elected probe is not holding a lock"

    before = await replica_c.snapshot()
    assert before.state == HALF_OPEN

    # A's straggler success finally lands, describing the dependency as it was
    # before the outage was even detected.
    await breaker.record_success(probe=decision_a.probe)

    after = await replica_c.snapshot()
    assert after.state == HALF_OPEN, (
        "a straggler success closed a half-open circuit on stale evidence, "
        "pre-empting the elected probe's verdict"
    )
    assert after.failures == before.failures, "the straggler reset the failure count"

    assert await replica_c._redis().get(replica_c.probe_key) == lock_token, (
        "the straggler released the elected probe's lock"
    )

    # The consequence the lock exists to prevent: with it gone, another replica
    # would be admitted to probe concurrently.
    intruder = await replica_b.allow()
    assert intruder.allowed is False, (
        "a second replica was admitted to probe while C's probe was still in "
        "flight -- the half-open probe is no longer single-flight"
    )

    # And C, the elected probe, still decides recovery for everyone.
    await replica_c.record_success(probe=True)
    assert await replica_c.state() == CLOSED


async def test_partially_degraded_result_is_a_success(breaker, retrieval_stub):
    """One dead path is still a useful answer. It must NOT trip the breaker.

    The counterpart to the test above, and the reason `_total_failure` compares
    sets rather than just asking "was anything degraded?". Opening the circuit
    because one path was briefly unwell would discard the other path's real
    results -- converting a partial degradation into a total one.
    """
    from retrieve.types import HybridResult, KEYWORD, SEMANTIC

    candidates = list(retrieval_stub.candidates)

    async def keyword_path_dead(_query):
        return HybridResult(
            candidates=candidates,
            degraded={KEYWORD: "OperationalError: refused"},
            path_counts={SEMANTIC: len(candidates), KEYWORD: 0},
            elapsed_ms=120.0,
        )

    for _ in range(THRESHOLD + 2):
        result = await guarded_hybrid_search(
            a_query(), breaker=breaker, search_fn=keyword_path_dead
        )
        assert result.candidates == candidates

    final = await breaker.snapshot()
    assert final.state == CLOSED
    assert final.failures == 0
