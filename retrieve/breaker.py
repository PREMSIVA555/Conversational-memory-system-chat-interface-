"""A Redis-backed circuit breaker for the retrieval read path (plan steps 1-4, 15).

                       N consecutive failures
          ┌────────┐ ─────────────────────────> ┌──────┐
          │ closed │                            │ open │
          └────────┘ <───────────────────────── └──────┘
               ^        probe succeeded            │
               │                                   │ COOLDOWN_SECONDS
               │        ┌───────────┐              │ elapsed since opened_at
               └─────── │ half_open │ <────────────┘
                        └───────────┘
                              │  probe failed
                              └──────────────────> open (cooldown restarts)


WHY THE STATE LIVES IN REDIS AND NOT IN THIS PROCESS
----------------------------------------------------
A breaker whose counter is a Python attribute protects one replica. Run three
API pods behind a load balancer and a failing Postgres has to be discovered
three separate times, each pod paying its own N failed retrievals before it
stops trying — and every pod re-learns it after each deploy. Worse, the state is
invisible: there is no way to answer "is memory currently degraded?" without
attaching a debugger to a specific process.

So the entire state record — `state`, `failures`, `opened_at` — is a single JSON
string under ONE namespaced Redis key (`retrieve/config.py:BREAKER_REDIS_KEY`).
Any replica that reads it sees what every other replica has learned, and a human
can read it too:

    redis-cli GET memsys:breaker:retrieval
    {"state":"open","failures":3,"opened_at":1788184579.6882}

(a real captured value — note `cjson` drops a trailing `.0`, so a whole-second
timestamp comes back as `1788184579`, not `1788184579.0`)

A string (not a hash) on purpose: `GET` on a hash is a WRONGTYPE error, and the
whole point of a single inspectable key is that the obvious command works.

WHY EVERY MUTATION IS A LUA SCRIPT (plan step 4)
------------------------------------------------
Read-modify-write over a shared counter from N replicas is the textbook lost
update. Two pods both read `failures=2`, both write `3`, and the third failure
that should have opened the circuit is silently swallowed. Redis executes a Lua
script atomically against the whole keyspace, so read, decide and write happen
with no interleaving — no `WATCH` retry loop, no lost increments, and the state
machine's transitions are decided *inside* the same atomic step that persists
them. `EVALSHA` with an `EVAL` fallback keeps the wire cost to a hash.

WHY IT FAILS **OPEN** WHEN REDIS ITSELF IS DOWN
-----------------------------------------------
`allow()` returns `allowed=True` on any Redis error. This is the single most
important line in the module and it is deliberately the opposite of what
"fail-safe" usually means.

The breaker exists to stop a sick memory layer from hurting the reply. If its
own bookkeeping store is unreachable and it responded by blocking retrieval —
or worse, by raising — then losing Redis would degrade or break chat for
everyone, and the component installed to contain an outage would be causing
one. Unavailable bookkeeping means "I do not know", and the safe answer to "I do
not know" here is to let the call through: retrieval has its own timeout, so the
worst case is a slow-but-bounded call rather than a dead endpoint.

CLOCK INJECTION (plan step 15)
------------------------------
`now()` is a constructor argument. Cooldown expiry is therefore tested by
advancing a fake clock, not by sleeping 30 seconds — which would make the suite
slow *and* flaky at the boundary. The clock is passed into the Lua scripts as an
argument rather than read via Redis `TIME`, so the injected value is the one the
state machine actually uses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from retrieve import config
from store.db import redis_url

logger = logging.getLogger(__name__)

__all__ = [
    "CLOSED",
    "OPEN",
    "HALF_OPEN",
    "BreakerDecision",
    "BreakerState",
    "CircuitBreaker",
    "get_breaker",
    "reset_breaker",
    "BREAKER_STATE_GAUGE",
    "STATE_VALUES",
]

# --- the three states -------------------------------------------------------

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

#: Numeric encoding for the Prometheus gauge (plan step 13). Ordered by
#: severity so a dashboard threshold like `> 0` means "not healthy".
STATE_VALUES = {CLOSED: 0.0, HALF_OPEN: 1.0, OPEN: 2.0}


# ---------------------------------------------------------------------------
# metrics (plan step 13)
# ---------------------------------------------------------------------------

def get_or_create_metric(factory: Callable[[], Any], name: str) -> Any:
    """Build a Prometheus collector, tolerating a double import.

    `prometheus_client`'s default registry raises `ValueError: Duplicated
    timeseries` if the same metric name is registered twice, and a module CAN be
    imported twice in one process — most reliably under pytest, where a test
    reloads a module, and under `python -m api.main`, which is exactly the trap
    `api/main.py` documents for its uvicorn invocation. A metric definition
    blowing up an import is a far worse outcome than reusing the existing
    collector, so on collision we look the registered one up and return it.
    """
    try:
        return factory()
    except ValueError:
        from prometheus_client import REGISTRY

        # `_names_to_collectors` is private but it is the only lookup the client
        # exposes for "give me the collector already registered under this name".
        existing = REGISTRY._names_to_collectors.get(name)
        if existing is None:  # pragma: no cover - collision implies presence
            raise
        return existing


def _build_state_gauge():
    from prometheus_client import Gauge

    return Gauge(
        "memsys_breaker_state",
        "Retrieval circuit breaker state (0=closed, 1=half_open, 2=open)",
        ["circuit"],
    )


BREAKER_STATE_GAUGE = get_or_create_metric(_build_state_gauge, "memsys_breaker_state")


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class BreakerState:
    """The record as it is stored in Redis."""

    state: str = CLOSED
    failures: int = 0
    opened_at: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.state == OPEN

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "failures": self.failures, "opened_at": self.opened_at}


@dataclass(frozen=True, slots=True)
class BreakerDecision:
    """What `allow()` tells the caller.

    allowed    may this caller run the protected operation?
    probe      is this caller the single-flight half-open probe? A probe's
               success/failure decides the circuit's fate for everyone, so the
               caller has to hand this flag back to `record_*` in order for the
               half_open -> closed / half_open -> open edges to fire.
    state      the state observed at decision time.
    blind      True when Redis could not be consulted. `allowed` is True in that
               case — see the module docstring on failing open.
    """

    allowed: bool
    probe: bool = False
    state: str = CLOSED
    blind: bool = False


# ---------------------------------------------------------------------------
# the Lua scripts (plan step 4) — every mutation is one atomic step
# ---------------------------------------------------------------------------

#: Shared prologue: decode the state record, defaulting a missing/corrupt one to
#: a healthy closed circuit. A breaker that refused to work because its own key
#: held junk would be another way to take down the reply path.
_DECODE = """
local raw = redis.call('GET', KEYS[1])
local state = 'closed'
local failures = 0
local opened_at = 0
if raw then
  local ok, decoded = pcall(cjson.decode, raw)
  if ok and type(decoded) == 'table' then
    state = decoded['state'] or 'closed'
    failures = tonumber(decoded['failures']) or 0
    opened_at = tonumber(decoded['opened_at']) or 0
  end
end
"""

_ENCODE = """
local function persist(s, f, o)
  redis.call('SET', KEYS[1], cjson.encode({state = s, failures = f, opened_at = o}))
end
"""

#: ALLOW — the read path's gate. KEYS[1]=state key, KEYS[2]=probe lock.
#: ARGV: 1=now, 2=cooldown_seconds, 3=probe_lock_ttl_ms, 4=probe token.
#: Returns {state, failures, verdict} where verdict is 0=blocked, 1=allowed,
#: 2=allowed-as-probe.
#:
#: The open -> half_open transition and the probe-lock acquisition happen inside
#: this one script, which is what makes the probe single-flight (plan step 3):
#: two replicas whose cooldowns expire in the same millisecond both reach the
#: `SET NX`, and Redis serializes the scripts, so exactly one gets the lock and
#: the other is told to stay blocked.
_ALLOW_LUA = (
    _DECODE
    + _ENCODE
    + """
local now = tonumber(ARGV[1])
local cooldown = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])

if state == 'open' then
  if (now - opened_at) >= cooldown then
    state = 'half_open'
    persist(state, failures, opened_at)
  else
    return {state, tostring(failures), '0'}
  end
end

if state == 'half_open' then
  if redis.call('SET', KEYS[2], ARGV[4], 'NX', 'PX', ttl) then
    return {state, tostring(failures), '2'}
  end
  return {state, tostring(failures), '0'}
end

return {state, tostring(failures), '1'}
"""
)

#: FAILURE — increment, and open if that crosses the threshold.
#: ARGV: 1=now, 2=threshold, 3="1" when the caller held the probe.
#: A failure while half_open re-opens immediately and RESTARTS the cooldown from
#: `now`, regardless of the counter: the probe is the whole evidence gathered in
#: that state, and it said the dependency is still sick.
_FAILURE_LUA = (
    _DECODE
    + _ENCODE
    + """
local now = tonumber(ARGV[1])
local threshold = tonumber(ARGV[2])
local was_probe = ARGV[3] == '1'

failures = failures + 1

if state == 'half_open' or was_probe then
  state = 'open'
  opened_at = now
  if failures < threshold then failures = threshold end
elseif failures >= threshold then
  state = 'open'
  opened_at = now
end

persist(state, failures, opened_at)
redis.call('DEL', KEYS[2])
return {state, tostring(failures)}
"""
)

#: SUCCESS — close and reset, but only when this success is actually evidence
#: about the *current* state. ARGV: 1="1" when the caller held the probe.
#: Returns {state, failures, applied} where applied is '1' if it closed.
#:
#: Closing on a `closed`-state success is what makes the failure count
#: *consecutive* (the plan's word): one success wipes the counter, so N-1
#: failures spread across healthy calls never accumulate into an open circuit.
#:
#: THE STRAGGLER GUARD — why this is not the unconditional `persist('closed')`
#: it used to be. In a fleet, a call can pass `allow()` while the circuit is
#: closed, take a few seconds, and land its success *after* other replicas have
#: seen N failures and opened the circuit. That success is stale evidence: it
#: describes the dependency as it was before the outage was detected. Applied
#: unconditionally it slams an open circuit straight back to closed, discarding
#: the cooldown and every other replica's findings — and the more replicas
#: there are, the likelier it is, which is precisely backwards for a mechanism
#: that exists to coordinate them.
#:
#: So a success only closes the circuit when it is either (a) observed while
#: still `closed` — an ordinary counter reset — or (b) the half-open probe,
#: which is the one call deliberately elected to decide recovery for everyone.
#: A straggler landing on `open` or `half_open` is dropped, and must not `DEL`
#: the probe lock either: that lock belongs to the elected probe, and releasing
#: it would let a second replica probe concurrently.
_SUCCESS_LUA = (
    _DECODE
    + _ENCODE
    + """
local was_probe = ARGV[1] == '1'

if was_probe or state == 'closed' then
  persist('closed', 0, 0)
  redis.call('DEL', KEYS[2])
  return {'closed', '0', '1'}
end

return {state, tostring(failures), '0'}
"""
)


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


_no_script_error: Optional[type] = None


def _noscript_exception() -> type:
    """`redis.exceptions.NoScriptError`, resolved on first use and cached.

    Resolved lazily rather than imported at module scope so `retrieve.breaker`
    can still be imported (and its pure logic exercised) in an environment
    without `redis` installed.

    WHY A TYPE AND NOT A STRING MATCH — this replaces
    `if "NOSCRIPT" not in str(exc).upper()`, which was dead code from the day it
    was written. Redis's *server* replies with the error code `NOSCRIPT`, but
    redis-py does not surface that text: it raises `NoScriptError("No matching
    script. Please use EVAL.")`. The substring never appears, so the guard
    re-raised every time, the stale sha was never dropped, `EVAL` was never
    reached, and the exception fell through to `allow()`'s fail-open handler.

    The consequence was total and silent. After any `SCRIPT FLUSH` — which is
    what a Redis restart does to the script cache — every mutation this class
    makes would fail, the state would never leave `closed`, and the breaker
    would never open again for the life of the process. The only signal was a
    WARNING reading "Redis unavailable", which is actively misleading when
    Redis is up and healthy.

    The lesson is the general one: match the driver's typed exception, never its
    message text. The type is part of the API; the wording is not.
    """
    global _no_script_error
    if _no_script_error is None:
        from redis.exceptions import NoScriptError

        _no_script_error = NoScriptError
    return _no_script_error


# ---------------------------------------------------------------------------
# the breaker
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """One replica's handle on the shared, Redis-resident circuit state.

    Constructing a second instance against the same `redis_url` and `key` is
    exactly how a second replica is simulated: the instances share nothing in
    Python, only the Redis record. `tests/reliability/` relies on that, and it is
    the property that a process-local counter cannot fake.
    """

    def __init__(
        self,
        *,
        key: str | None = None,
        url: str | None = None,
        redis_client: Any | None = None,
        failure_threshold: int | None = None,
        cooldown_seconds: float | None = None,
        clock: Callable[[], float] | None = None,
        probe_lock_ttl_seconds: float | None = None,
        connect_timeout: float = 2.0,
        name: str = "retrieval",
    ) -> None:
        self.key = key or config.breaker_redis_key()
        self.probe_key = f"{self.key}:probe"
        self.name = name
        self._url = url or redis_url()
        self._client = redis_client
        self._owns_client = redis_client is None
        self._connect_timeout = connect_timeout
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        # Plan step 15: the injectable clock. Defaults to wall time.
        self.now: Callable[[], float] = clock or time.time
        # The probe lock must outlive one retrieval attempt, or a second replica
        # would grab it while the first probe is still in flight and the probe
        # would stop being single-flight. It must also expire, or a replica that
        # dies mid-probe would wedge the circuit in half_open forever. One
        # retrieval timeout plus a healthy margin satisfies both.
        self._probe_lock_ttl = (
            probe_lock_ttl_seconds
            if probe_lock_ttl_seconds is not None
            else config.retrieval_timeout_seconds() * 2 + 1.0
        )
        self._script_shas: dict[str, str] = {}

    # -- tunables, read late so a monkeypatched env var is honoured ---------

    @property
    def failure_threshold(self) -> int:
        if self._failure_threshold is not None:
            return self._failure_threshold
        return config.breaker_failure_threshold()

    @property
    def cooldown_seconds(self) -> float:
        if self._cooldown_seconds is not None:
            return self._cooldown_seconds
        return config.breaker_cooldown_seconds()

    # -- redis plumbing ----------------------------------------------------

    def _redis(self) -> Any:
        """The client for this instance, built lazily.

        Lazily because constructing a breaker must not require a reachable
        Redis: `test_redis_unavailable_fails_open_not_closed` builds one against
        a dead address and expects failures at *call* time, handled, rather than
        an exception at construction time.
        """
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(
                self._url,
                decode_responses=True,
                socket_connect_timeout=self._connect_timeout,
                socket_timeout=self._connect_timeout,
                retry_on_timeout=False,
            )
        return self._client

    async def _eval(self, name: str, script: str, keys: list[str], args: list[str]) -> list[str]:
        """Run one Lua script, preferring `EVALSHA` and falling back to `EVAL`.

        The fallback is not optional: Redis evicts its script cache on
        `SCRIPT FLUSH`, on `FLUSHALL` and on restart, and a cached sha that no
        longer resolves makes redis-py raise `NoScriptError`. Re-loading the body
        on that error is what keeps a long-lived process working across a Redis
        restart — see `_noscript_exception()` for what happened when this branch
        was guarded by a string match instead of the type.

        `EVAL` also re-caches the script server-side, so the stored sha becomes
        valid again and only the first call after a flush pays the extra
        round-trip.
        """
        client = self._redis()
        sha = self._script_sha(name, script)
        try:
            if sha is None:
                loaded = await client.script_load(script)
                self._script_shas[name] = _text(loaded)
                sha = self._script_shas[name]
            return await client.evalsha(sha, len(keys), *keys, *args)
        except _noscript_exception():
            logger.info(
                "circuit breaker: script cache lost the %s script (Redis restart or "
                "SCRIPT FLUSH); re-sending the body with EVAL",
                name,
            )
            self._script_shas.pop(name, None)
            return await client.eval(script, len(keys), *keys, *args)

    def _script_sha(self, name: str, script: str) -> Optional[str]:
        return self._script_shas.get(name)

    async def aclose(self) -> None:
        """Release the client, if this instance built it.

        pytest-asyncio closes the event loop after every test and a redis client
        binds to the loop it was created on, so leaving one behind is the same
        class of bug `tests/conftest.py` exists to prevent for Postgres pools.
        """
        if self._client is not None and self._owns_client:
            try:
                await self._client.aclose()
            except Exception:  # pragma: no cover - teardown must not raise
                pass
        if self._owns_client:
            self._client = None
            self._script_shas.clear()

    # -- reads -------------------------------------------------------------

    async def snapshot(self) -> BreakerState:
        """The stored record, verbatim. No transitions, no side effects.

        This is what a second replica calls to observe what the first one
        learned, and what the DoD's `redis-cli GET` shows.
        """
        try:
            raw = await self._redis().get(self.key)
        except Exception as exc:  # noqa: BLE001 - unknown state is not open
            logger.warning("circuit breaker: cannot read state from Redis (%s: %s)", type(exc).__name__, exc)
            return BreakerState()
        if not raw:
            return BreakerState()
        try:
            decoded = json.loads(_text(raw))
            return BreakerState(
                state=str(decoded.get("state", CLOSED)),
                failures=int(decoded.get("failures", 0) or 0),
                opened_at=float(decoded.get("opened_at", 0.0) or 0.0),
            )
        except (ValueError, TypeError):
            logger.warning("circuit breaker: state key %s holds junk; treating as closed", self.key)
            return BreakerState()

    async def state(self) -> str:
        """The stored state string — `closed`, `open` or `half_open`."""
        return (await self.snapshot()).state

    # -- the gate ----------------------------------------------------------

    async def allow(self) -> BreakerDecision:
        """Decide whether this caller may run the protected operation.

        Performs the `open -> half_open` transition when the cooldown has
        elapsed, and acquires the single-flight probe lock, atomically.
        """
        try:
            raw = await self._eval(
                "allow",
                _ALLOW_LUA,
                [self.key, self.probe_key],
                [
                    repr(float(self.now())),
                    repr(float(self.cooldown_seconds)),
                    str(int(self._probe_lock_ttl * 1000)),
                    uuid.uuid4().hex,
                ],
            )
        except Exception as exc:  # noqa: BLE001
            # FAIL OPEN. See the module docstring — this is the deliberate one.
            logger.warning(
                "circuit breaker: Redis unavailable (%s: %s); allowing the call through "
                "rather than blocking it — bookkeeping being down must never take the "
                "reply path with it",
                type(exc).__name__,
                exc,
            )
            self._observe(CLOSED)
            return BreakerDecision(allowed=True, probe=False, state=CLOSED, blind=True)

        state, _failures, verdict = _text(raw[0]), _text(raw[1]), _text(raw[2])
        self._observe(state)
        return BreakerDecision(
            allowed=verdict != "0",
            probe=verdict == "2",
            state=state,
        )

    # -- outcome recording -------------------------------------------------

    async def record_success(self, *, probe: bool = False) -> BreakerState:
        """Close the circuit and reset the consecutive-failure count.

        Ignored when this success is a straggler — a call that was admitted
        while the circuit was closed but finished after other replicas opened it.
        See `_SUCCESS_LUA` for why that case must not re-close the circuit.
        """
        try:
            raw = await self._eval(
                "success", _SUCCESS_LUA, [self.key, self.probe_key], ["1" if probe else "0"]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "circuit breaker: could not record success (%s: %s)", type(exc).__name__, exc
            )
            return BreakerState()

        state, failures, applied = _text(raw[0]), int(_text(raw[1])), _text(raw[2]) == "1"
        if probe and applied:
            logger.info("circuit breaker %s: half-open probe succeeded; circuit closed", self.name)
        elif not applied:
            logger.info(
                "circuit breaker %s: ignoring a straggler success that completed after the "
                "circuit went %s; it describes the dependency before the outage was detected",
                self.name,
                state,
            )
        self._observe(state)
        return BreakerState(
            state=state,
            failures=failures,
            opened_at=0.0 if applied else (await self.snapshot()).opened_at,
        )

    async def record_failure(self, *, probe: bool = False, reason: str = "") -> BreakerState:
        """Count one failure, opening the circuit if that crosses the threshold."""
        try:
            raw = await self._eval(
                "failure",
                _FAILURE_LUA,
                [self.key, self.probe_key],
                [
                    repr(float(self.now())),
                    str(int(self.failure_threshold)),
                    "1" if probe else "0",
                ],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "circuit breaker: could not record failure (%s: %s)", type(exc).__name__, exc
            )
            return BreakerState()

        state, failures = _text(raw[0]), int(_text(raw[1]))
        self._observe(state)
        if state == OPEN:
            logger.warning(
                "circuit breaker %s OPEN after %d consecutive failures (%s); retrieval is "
                "skipped for the next %.0fs and replies continue without memory",
                self.name,
                failures,
                reason or "no reason given",
                self.cooldown_seconds,
            )
        return BreakerState(state=state, failures=failures, opened_at=self.now() if state == OPEN else 0.0)

    # -- administration ----------------------------------------------------

    async def reset(self) -> None:
        """Delete the shared record. Test setup and manual recovery."""
        try:
            await self._redis().delete(self.key, self.probe_key)
        except Exception:  # pragma: no cover
            pass
        self._observe(CLOSED)

    async def force_open(self) -> BreakerState:
        """Trip the circuit directly, without N real failures.

        Used by tests that need an open circuit as a *precondition* rather than
        as the thing under test, and by an operator who wants to shed the memory
        layer deliberately.
        """
        threshold = self.failure_threshold
        record = {"state": OPEN, "failures": threshold, "opened_at": float(self.now())}
        await self._redis().set(self.key, json.dumps(record))
        await self._redis().delete(self.probe_key)
        self._observe(OPEN)
        return BreakerState(**record)

    def _observe(self, state: str) -> None:
        BREAKER_STATE_GAUGE.labels(circuit=self.name).set(STATE_VALUES.get(state, 0.0))


# ---------------------------------------------------------------------------
# process default
# ---------------------------------------------------------------------------

_default: Optional[tuple[Any, CircuitBreaker]] = None


def get_breaker() -> CircuitBreaker:
    """The breaker `retrieve/guarded.py` uses when no other is supplied.

    Cached **per event loop**, not per process. A redis client binds to the loop
    it was built on, and pytest-asyncio builds a fresh loop for every test
    function; a plain module singleton would hand test 2 a client whose loop is
    closed. Rebinding on loop change is the same defence `tests/conftest.py`
    applies to Postgres pools, applied where it belongs — in the component that
    owns the connection.
    """
    global _default
    try:
        loop: Any = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if _default is not None:
        cached_loop, breaker = _default
        if cached_loop is loop and (loop is None or not loop.is_closed()):
            return breaker

    breaker = CircuitBreaker()
    _default = (loop, breaker)
    return breaker


async def reset_breaker() -> None:
    """Drop the cached default breaker, closing its client."""
    global _default
    if _default is not None:
        _, breaker = _default
        await breaker.aclose()
    _default = None
