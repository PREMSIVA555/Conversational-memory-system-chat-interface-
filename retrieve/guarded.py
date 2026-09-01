"""Retrieval with a timeout and a circuit breaker around it (plan step 5).

    caller ─> breaker.allow() ─┬─ blocked ──────────> RetrievalUnavailable
                               │
                               └─ allowed ─> wait_for(hybrid_search, T) ─┬─ ok ──> record_success
                                                                        └─ bad ─> record_failure
                                                                                  + RetrievalUnavailable

THREE LAYERS OF TIMEOUT, AND WHY THAT IS NOT REDUNDANT
------------------------------------------------------
`retrieve/hybrid.py` already gives each path its own `asyncio.wait_for` and its
own `except`, so one sick path degrades to an empty list while the other still
returns results. That is deliberately *forgiving*: a half-degraded retrieval is
worth more to the user than an exception.

But forgiveness has no memory. Hybrid search will keep paying 5 seconds for a
dead Postgres on every single turn, forever, and report it as a mild degradation
each time. This module is the layer that notices the pattern and stops paying:

  * `RETRIEVAL_TIMEOUT_MS` bounds the *whole* call, including the merge and pool
    checkout, so nothing downstream of the per-path budgets can hang the reply.
  * The breaker counts consecutive failures across replicas and, after N, stops
    calling retrieval at all until a cooldown has passed.

WHAT COUNTS AS A FAILURE
------------------------
A timeout, a raised exception, or a result in which **every** attempted path
degraded. A result in which *some* paths degraded is a success.

The partial case is a success because M3's per-path isolation means it still
contains real memories and still returned promptly. Counting it as a breaker
failure would open the circuit whenever one path was briefly unwell and throw
away the other path's working results — turning a partial degradation into a
total one, which is the exact opposite of what a breaker is for.

The total case has to be a failure, and missing it would have left a hole
exactly where the breaker matters most. `hybrid_search` never raises: it
converts a dead dependency into an empty list plus a `degraded` entry. So with
Postgres stopped, *both* paths fail, and the call returns — late, empty, and
technically successful. A breaker that only watched for exceptions would stay
closed through a total database outage and keep paying the full per-path timeout
on every single turn, forever. Adding a second's worth of latency to every reply
for an outage it was installed to notice.

`_total_failure()` below distinguishes the two by comparing the degraded paths
against the attempted ones. Note that an empty candidate list on its own is NOT
a failure — a cold memory store legitimately returns nothing, and treating "this
user has no memories yet" as a dependency outage would open the circuit on every
new user.

WHY IT RAISES INSTEAD OF RETURNING EMPTY
----------------------------------------
`RetrievalUnavailable` is typed and explicit so the response graph must handle
it deliberately (setting `degraded=True`) rather than silently mistaking "the
memory layer is down" for "this user has no memories". Those two states look
identical in an empty candidate list and mean completely different things to the
audit log M7 builds on top.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from retrieve import config
from retrieve import hybrid as hybrid_module
from retrieve.breaker import CircuitBreaker, get_or_create_metric
from retrieve.types import HybridResult, RetrievalQuery

logger = logging.getLogger(__name__)

__all__ = [
    "RetrievalUnavailable",
    "guarded_hybrid_search",
    "RETRIEVAL_TIMEOUTS",
    "RETRIEVAL_BLOCKED",
]


# ---------------------------------------------------------------------------
# metrics (plan step 13)
# ---------------------------------------------------------------------------

def _build_timeout_counter():
    from prometheus_client import Counter

    return Counter(
        "memsys_retrieval_timeouts_total",
        "Guarded retrieval calls that exceeded RETRIEVAL_TIMEOUT_MS",
    )


def _build_blocked_counter():
    from prometheus_client import Counter

    return Counter(
        "memsys_retrieval_blocked_total",
        "Guarded retrieval calls short-circuited by an open breaker",
    )


RETRIEVAL_TIMEOUTS = get_or_create_metric(
    _build_timeout_counter, "memsys_retrieval_timeouts_total"
)
RETRIEVAL_BLOCKED = get_or_create_metric(
    _build_blocked_counter, "memsys_retrieval_blocked_total"
)


# ---------------------------------------------------------------------------
# the typed failure
# ---------------------------------------------------------------------------

class RetrievalUnavailable(Exception):
    """Retrieval could not be completed and no candidates are available.

    `reason` is one of:

        open_circuit         we chose not to try
        timeout              we tried and it exceeded the budget
        error                we tried and it raised
        all_paths_degraded   we tried, it returned, and every path had failed

    A caller can therefore distinguish "we chose not to try" from the three ways
    of "we tried and it broke" without parsing a message. `circuit_state`
    records what the breaker thought at the time, which is the piece an operator
    actually wants in the log line.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        circuit_state: str = "closed",
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.circuit_state = circuit_state
        self.__cause__ = cause


# ---------------------------------------------------------------------------
# the guarded call
# ---------------------------------------------------------------------------

SearchFn = Callable[[RetrievalQuery], Awaitable[HybridResult]]


def _total_failure(result: HybridResult) -> bool:
    """True when every path `hybrid_search` attempted degraded.

    `path_counts` is keyed by the paths that were actually run, so it — not a
    hardcoded `{semantic, keyword}` — is the right denominator: add a third
    retrieval path later and this keeps working without being edited.

    Returns False for a blank query, whose short-circuit produces no degradation
    and no attempted paths.
    """
    attempted = set(result.path_counts)
    if not attempted or not result.degraded:
        return False
    return attempted.issubset(set(result.degraded))


async def guarded_hybrid_search(
    query: RetrievalQuery,
    *,
    breaker: Optional[CircuitBreaker] = None,
    timeout_ms: int | None = None,
    search_fn: Optional[SearchFn] = None,
) -> HybridResult:
    """Run hybrid retrieval behind the breaker and the wall-clock budget.

    Raises `RetrievalUnavailable` when the circuit is open, when the call
    exceeds the budget, or when it raises. Returns the `HybridResult` otherwise
    — including a partly-degraded one, which is a success (see module docstring).

    `search_fn` exists so a test can substitute a controllable retrieval without
    monkeypatching module globals; when omitted the attribute is looked up on
    `retrieve.hybrid` at call time, so `monkeypatch.setattr` on that module works
    too.
    """
    if breaker is None:
        from retrieve.breaker import get_breaker

        breaker = get_breaker()

    decision = await breaker.allow()

    if not decision.allowed:
        RETRIEVAL_BLOCKED.inc()
        logger.info(
            "retrieval skipped: circuit is %s; replying without memory", decision.state
        )
        raise RetrievalUnavailable(
            "open_circuit",
            f"retrieval circuit is {decision.state}; not attempting a call",
            circuit_state=decision.state,
        )

    budget = (
        config.retrieval_timeout_seconds() if timeout_ms is None else float(timeout_ms) / 1000.0
    )
    # Late binding on the module attribute, matching how `retrieve/hybrid.py`
    # itself resolves its two path functions — it is what makes monkeypatching
    # work in tests.
    run = search_fn or hybrid_module.hybrid_search

    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(run(query), timeout=budget)
    except asyncio.TimeoutError as exc:
        RETRIEVAL_TIMEOUTS.inc()
        state = await breaker.record_failure(
            probe=decision.probe, reason=f"timeout after {budget * 1000:.0f}ms"
        )
        raise RetrievalUnavailable(
            "timeout",
            f"retrieval exceeded its {budget * 1000:.0f}ms budget",
            circuit_state=state.state,
            cause=exc,
        ) from exc
    except asyncio.CancelledError:
        # Never counted and never swallowed: cancellation is the caller going
        # away (client disconnect, shutdown), not the dependency failing.
        raise
    except Exception as exc:  # noqa: BLE001 - every failure mode is one strike
        state = await breaker.record_failure(
            probe=decision.probe, reason=f"{type(exc).__name__}: {exc}"
        )
        raise RetrievalUnavailable(
            "error",
            f"retrieval failed: {type(exc).__name__}: {exc}",
            circuit_state=state.state,
            cause=exc,
        ) from exc

    if _total_failure(result):
        detail = "; ".join(f"{path}: {why}" for path, why in sorted(result.degraded.items()))
        state = await breaker.record_failure(
            probe=decision.probe, reason=f"every retrieval path degraded ({detail})"
        )
        raise RetrievalUnavailable(
            "all_paths_degraded",
            f"every retrieval path degraded, so nothing could be retrieved ({detail})",
            circuit_state=state.state,
        )

    await breaker.record_success(probe=decision.probe)
    logger.debug(
        "guarded retrieval ok in %.0fms (%d candidates, circuit %s)",
        (time.perf_counter() - started) * 1000,
        len(result.candidates),
        decision.state,
    )
    return result


async def guarded_retrieve(
    query: RetrievalQuery,
    **kw: Any,
) -> list:
    """Candidates only, for callers that do not inspect per-path degradation."""
    return (await guarded_hybrid_search(query, **kw)).candidates
