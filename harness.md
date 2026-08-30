# harness.md — orchestration log

This file is the **orchestrator's** running log: what I decided, what I dispatched, what
came back. It is written by the orchestrating session, not by the milestone agents.

For milestone status at a glance, see [kickoff.md](kickoff.md).
For the specification being built, see [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

---

## Roles

| Role | Who | Responsibility |
| --- | --- | --- |
| Orchestrator | This Claude Code session | Waves, dispatch, contracts, conflict prevention, logging |
| Builder agent | One subagent per milestone | Implements that milestone's steps + tests, ticks its own boxes |
| Verifier agent | A **separate** subagent per milestone | Independently re-runs the Definition of Done. Never the builder. |
| User | You | Final sign-off. Only you fill in `**Milestone signed off by user on:**` |

**Rule: a builder's checked box is a claim, not completion.** A milestone is marked
`Verified` in kickoff.md only after a fresh verifier agent — one that did not write the
code — re-runs every Definition of Done line from the plan and reports pass.

---

## Environment probe (2026-08-29)

| Check | Result |
| --- | --- |
| Docker | 29.7.2, daemon up, 0 containers |
| Docker Compose | v5.4.0 |
| Python | 3.11.8 (also 3.14.3 on PATH — pin 3.11 for the venv) |
| Node / npm | v22.23.0 / 12.0.2 |
| git | 2.54.0 — repo initialized this session (was not a repo) |
| Ports 5432/6379/9000/9001/9090/3000/8000 | assumed free; M1 agent confirms |
| LLM provider key | Groq, supplied by user, verified live |

---

## Decision log

### D1 — LLM provider: Groq
User supplied a Groq key. Verified against `GET /openai/v1/models` and a live completion.

- `LLM_MODEL=groq/openai/gpt-oss-120b` — verified `content=[OK] finish=stop`.
- Rejected `groq/compound`: ignores terse instructions and pads output with meta-commentary,
  which is bad for the structured JSON extraction M2 needs.
- `qwen/qwen3.8-27b` also verified clean — recorded as the fallback model.
- Note for M2: gpt-oss models spend budget on reasoning tokens before emitting content.
  `max_tokens` must be generous (>=512) or `content` comes back empty with `finish=stop`.
  This cost me one confusing probe; it is written into the M2 brief so it costs nobody else.

### D2 — Embeddings: Voyage AI
Groq exposes **no embeddings endpoint** (its model list is chat + audio only), so the read
path needs a second provider. User supplied a Voyage AI key; verified live against
`POST /v1/embeddings`.

- `EMBEDDING_MODEL=voyage/voyage-3.5`, `EMBEDDING_DIM=1024`.
- `voyage-3.5`, `voyage-3.5-lite` and `voyage-3-large` all verified working and all return
  1024 dims. Took `voyage-3.5` for the retrieval-quality/cost balance; the other two are
  drop-in swaps at the same dimension, so changing between them needs **no migration**.
- LiteLLM routes this natively via the `voyage/` prefix and `VOYAGE_API_KEY`, so completions
  (Groq) and embeddings (Voyage) stay behind the single `llm/config.py` seam the plan
  requires — the two-provider split is invisible to every caller.
- 1024 dims is comfortably inside pgvector's 2000-dim ceiling for HNSW indexes.
- `EMBEDDING_DIM` is still templated into the `vector(N)` column at migration time rather
  than hardcoded, so a future move to a different-width embedder is an env change plus a
  re-embed, not a hand-edited migration.

*(Superseded: an earlier draft of this decision used local `fastembed`/ONNX embeddings to
avoid a second key. Dropped once the user supplied Voyage — a hosted, retrieval-tuned
embedder is a straight upgrade in quality for a memory system whose whole value is recall.)*

### D3 — Demo pulled forward (user decision)
The plan puts the first visible artifact at M6, five backend milestones deep. User chose to
insert an unnumbered **M2.5**: a thin Next.js chat page hitting the chat endpoint, so the
project is demoable from the first week. M6 later upgrades that same app in place with
streaming and the memory panel. No Definition of Done is weakened; M2.5 adds a checkpoint
rather than moving one.

### D4 — Secret handling
The Groq key goes only into `infra/.env`, which is gitignored before the first commit.
`infra/.env.example` carries the variable names with empty values. No key is ever written
into a tracked file, a test fixture, or an agent prompt beyond the one that provisions `.env`.

> Rotate this key when the project is done — it was pasted into a chat transcript, so treat
> it as disclosed.

### D5 — File ownership to prevent parallel-write collisions
Agents in the same wave never share a writable file. Ownership is assigned per wave below.
Where the plan would have two parallel agents touch one file (e.g. both M3 and M4 adding to
`retrieve/config.py`), the milestones are placed in **different** waves instead of splitting
the file, so the plan's structure is preserved exactly as written.

---

## Wave plan

Dependency-derived. Milestones in the same wave touch disjoint file sets.

| Wave | Milestones | Parallel? | Rationale |
| --- | --- | --- | --- |
| W1 | M1 | no | Foundation. Schema + LiteLLM wrapper + infra; everything imports it. |
| W2 | M2 ∥ M3 | **yes** | Disjoint trees: `capture/` + `graphs/capture*` vs `retrieve/` + `evals/`. M3 seeds its own eval fixtures, so it does not wait on M2's rows. |
| W3 | M2.5 ∥ M4 | **yes** | `frontend/` vs `retrieve/features,ranking` + `context/`. Zero overlap. |
| W4 | M5 | no | Needs M3 retriever **and** M4 composer; touches `retrieve/config.py` which M4 just wrote. |
| W5 | M7 | no | Governance hooks reach into the M2 write path and the M5 read path simultaneously. |
| W6 | M6 ∥ M8 | **yes** | `frontend/` vs `jobs/` + `evals/`. Fully disjoint. M7 landing first means M6 wires real endpoints and ships no mock module. |

Sequential-only path is M1 → M2/M3 → M4 → M5 → M7 → M6/M8: six waves for eight milestones
plus the inserted demo.

---

## Dispatch log

Appended as waves run. Each entry: what was dispatched, what returned, what the independent
verifier found.

### W1 — M1: Schema, LiteLLM wrapper, Docker infra

- **Dispatched:** builder agent, brief covers plan steps 1–17, test cases, and D1/D2/D4 above.
- **Owns:** `infra/`, `store/`, `llm/`, `api/main.py`, `scripts/`, `pyproject.toml`, `.gitignore`, `README.md`
- **Builder returned:** claims 17/17 steps, 13/13 tests passing (11 named + 2 it added), 12/12 DoD lines run.
- **Verifier returned: PASS.** 12/12 DoD lines and 11/11 named tests independently re-run.
  6 defects found, none invalidating the milestone.
- **Status: ✅ verified.** Committed as `3026f2b`.

**The verification was real, not a rubber stamp.** Worth recording what the verifier actually
did, because it is the standard every later milestone gets held to:

- **Proved container-vs-native by fingerprinting, not by trusting the port number.** Container
  Postgres reports `16.15 (Debian) … inet_server_addr 172.18.0.2`; the native service is
  PostgreSQL **18 on Windows** and rejects the project credentials outright. Redis: container
  `7.4.11 / Linux WSL2` vs native Memurai `8.2.7 / Windows`. No ambiguity left.
- **Proved the demo script can fail.** Copied it, forced the postgres port to a dead 59999, and
  got `passed: 18 / failed: 1` with real exit 1. A script that always exits 0 proves nothing;
  this one has a working failure path.
- **Attacked RLS rather than reading the catalog.** Ran its own cross-subject insert/read as
  `memory_app`: cross-subject read → 0 rows, **mismatched actor alone** → 0 rows (so the actor
  half of the predicate is load-bearing, not decoration), unset-GUC session → 0 rows (fails
  closed), cross-subject INSERT → rejected by `WITH CHECK`. Then cleaned up its test rows.
- **Used a negative control to prove the LLM calls are not stubbed.** Invalid keys produced
  provider-specific error envelopes (`GroqException … invalid_api_key`,
  `VoyageException … Provided API key is invalid`) that no local mock would emit, plus a novel
  arithmetic prompt no fixture could hold.
- **Checked the tests for vacuous assertions,** confirming `test_rls_blocks_cross_subject_read`
  asserts its role is not superuser/BYPASSRLS/owner *before* testing the boundary.

### M1 defects and disposition

| # | Defect | Severity | Disposition |
| --- | --- | --- | --- |
| 1 | API process on :8000 was serving **stale code** — process started 46s before `api/main.py` was last modified (proved via an `on_event` deprecation warning in the log for code that no longer exists) | should-fix | **Fixed.** Restarted on current on-disk code; log now clean, `/health` 200. |
| 2 | `APP_DB_PASSWORD` defaulted in source to the same literal as the live value, so the RLS role's real password sat in tracked code | should-fix | **Fixed.** Default removed from `store/db.py` and `store/migrate.py`; both now raise when unset. Literal is gone from every tracked file. |
| 3 | Zero git commits — entire milestone uncommitted and unrecoverable | should-fix | **Fixed.** Baseline committed as `3026f2b` after a staged-diff secret scan. |
| 4 | `feedback` RLS scopes actor by **presence** (`IS NOT NULL`), not equality | cosmetic | **Accepted, deferred to M7.** Forced by the plan itself: step 11 gives `feedback` no `actor_id` column. `audit_log`, which has one, uses proper equality. Flagged in the M7 brief. |
| 5 | The no-hardcoded-model guard test only walks `*.py`, skips `tests/`, and skips comment lines — narrower than step 14's "anywhere in the codebase" | cosmetic | **Accepted.** The verifier's own broader grep found no actual violation. Worth widening if it ever matters. |
| 6 | `demo_m1.sh` sources `infra/.env` with `set -a` unconditionally, overriding caller-exported vars | cosmetic | **Accepted.** Ergonomics only; affects no DoD line. |

Verifier honestly reported three things it could **not** confirm: that the native services are
unstoppable without elevation (it declined to stop services on the user's machine — correct
call), and that Grafana/Prometheus render in a real browser (it verified via HTTP status,
`<title>`, and `/api/health` instead). It also went *beyond* the plan and confirmed Prometheus
is genuinely scraping — target `health=up`, live `memsys_dependency_up` samples — where the
plan only asked that the page load.

### D8 — Git policy (user decision)
Commit after each **verified** milestone. Rationale: with six waves of parallel agents, a
per-milestone commit is the recovery point if one agent corrupts a shared file. Every commit
is preceded by a staged-diff scan for `gsk_`/`pa-` secret prefixes.

---

### W2 — M2 (capture graph) ∥ M3 (hybrid retrieval + evals)

Dispatched in parallel. Disjoint ownership enforced by brief:

| | M2 | M3 |
| --- | --- | --- |
| Owns | `capture/`, `graphs/capture*`, `store/memories.py`, `api/chat.py`, `tests/integration/conftest.py` | `retrieve/`, `evals/`, `api/retrieval_service.py`, own test modules |
| Forbidden | `retrieve/`, `evals/` | `capture/`, `graphs/`, `api/main.py`, `tests/integration/conftest.py` |

Three collision points resolved in advance rather than discovered at merge:

1. **`tests/integration/conftest.py`** — plan step M2.15 assigns it to M2. M3 is told to define
   fixtures inside its own test modules instead.
2. **`api/main.py`** — M2 may add a router include (two lines); M3 puts its plan-step-15
   "internal function" in a new `api/retrieval_service.py` and never opens `main.py`.
3. **Shared foundation files** (`store/db.py`, `llm/config.py`, migrations) — **neither** agent
   may edit them. Both are instructed to report needed changes to me instead. This is the rule
   that keeps parallelism from corrupting the build.

M3 does not wait on M2: plan step M3.10 has it seed its own eval fixtures deterministically, so
the two are genuinely independent despite M3 logically consuming M2's output in production.

- **Incident:** both agents were killed mid-flight by a session rate limit (HTTP 429), not by
  any fault in their work. Resumed after the limit reset.

**State at the kill, surveyed before resuming** (rather than assuming, since a half-written
tree is exactly where a cold restart silently duplicates or clobbers work):

| Agent | Written before the kill | Still missing |
| --- | --- | --- |
| M2 | `capture/__init__.py`, `capture/config.py` only | everything else — `graphs/`, all nodes, `store/memories.py`, `api/chat.py`, all tests |
| M3 | all of `retrieve/` (types, semantic, keyword, hybrid, config, README), `evals/metrics.py`, `evals/fixtures/seed_memories.py` | `golden_set.jsonl`, `run_eval.py`, `results/`, `api/retrieval_service.py`, all three test modules |

Neither had ticked a plan box; neither had updated `pyproject.toml`.

**Resumed rather than restarted.** Sending each agent a message revives it from its own
transcript, so it keeps everything it had already reasoned out. A cold restart would have
re-derived the same decisions at full token cost and risked a second agent writing over the
first's files. Each resume carried the exact on-disk inventory above so neither burns calls
rediscovering its own state.

**The expensive setup survived the kill:** langgraph 0.2.60, presidio-analyzer/anonymizer
2.2.355, spaCy 3.8.16 with `en_core_web_sm`, tiktoken 0.14.0 are all installed. Verified
before resuming and both agents were told explicitly not to reinstall.

**One warning issued to M3:** it wrote all of `retrieve/` but died before testing any of it.
It was told to treat that code as unverified and prove it against the live DB first, rather
than resuming on the assumption that written means working.

- **M3 builder returned:** 15/15 steps, 12/12 named tests (18 items, `18 passed`), eval runner
  exits 0, baseline written. Verifier dispatched.
- **Status:** M3 📋 → under verification. M2 still in progress.

#### Two real bugs M3 found in files it correctly refused to touch

This is the file-ownership rule paying off: M3 hit both, reported them instead of patching
them, and I fixed them centrally. Had it "just fixed" them locally, M2 would have been
running against a different `store/db.py` than M3.

**Bug 1 — `store/db.py:get_pool()` leaked connection pools under concurrency.** Check-then-act
straddling an `await`: two concurrent first-touch callers both saw an empty cache, both
*opened* a pool, and only the last was recorded. The other was orphaned holding up to
`max_size` connections, invisible to `close_pools()`, for the life of the process. Latent
since M1 — `retrieve/hybrid.py` is simply the first code in the project to touch the DB
concurrently, which is exactly how this class of bug surfaces.

Fixed with per-DSN double-checked locking plus cleanup if `open()` fails or is cancelled.
Verified by reproducing the original race: **6 concurrent first-touch callers → 1 pool
constructed, 1 tracked, 1 distinct object returned** (was 2 constructed / 1 tracked at just 2
callers).

**Bug 2 — no 429 handling in `llm/config.py`, on a 3-requests-per-minute key.** The Voyage
no-payment tier allows **3 requests/minute**, metered per request. LiteLLM's own `num_retries`
does not honour `Retry-After` and backs off far too briefly for a per-minute window, so a 429
propagated to the caller and the work was lost.

The symptom was diagnostic: M1's embedding test **failed under parallel agents and passed on
every isolated re-run**. That signature — fails only under concurrency, passes alone — means a
shared external quota, not a flaky test. Worth remembering rather than chasing as a test bug.

Fixed with `_with_rate_limit_retry`: jittered exponential backoff that prefers the provider's
own `Retry-After`. Deliberately narrow — **only** 429 retries; a 401 or malformed request
raises on the first attempt rather than burning a minute hiding the real cause. Verified all
four behaviours:

| Behaviour | Result |
| --- | --- |
| Transient 429 then success | recovered after 3 calls / 13.1s |
| `Retry-After: 1` honoured over the 4.0s default | waited 1.1s |
| Non-429 (401) fails fast | raised after 1 call, 0.00s |
| Exhaustion | actionable error naming the 3-req/min tier |

Consequence worth noting: plan step M2.5 ("batch-embed the surviving candidates") is now a
**quota** requirement, not just an efficiency one. N separate embeds on this tier is N/3
minutes of backoff.

Regression check after both fixes: **31 passed** (M1's 13 + M3's 18), no failures.

#### M3's most valuable finding: four failed golden-query designs

The plan asks for a query "matchable only via keyword". M3 reports it measured **four designs
that all failed** before finding one that works, because Voyage-3.5 ranks any row containing a
literal query token #1 *regardless of surrounding meaning*:

| Design | Semantic rank of target | Verdict |
| --- | --- | --- |
| `Zbigniewicz`, short row | 1 | matchable by both — proves nothing |
| `Kowalczyk` in a 35-word row | 2 | still both |
| same + 9 competitor rows | 3 | still both |
| `Beethoven` + 6-row music cluster | 1 | still both |

What finally worked is a **stemming collision**: `organ` and `organic` both stem to `organ`, so
the keyword path hits an "organic vegetables" row while the embedder only ever sees grocery
shopping. Measured: keyword → `[veg]`, semantic → `[commute, openmic, strings, coffee,
dentist]`, target genuinely absent from the semantic path.

This matters because the naive version of this test — pick a rare token — **passes while
proving nothing**, since both paths return the row. The verifier is specifically tasked with
re-running the semantic path alone to confirm the target is genuinely absent.

#### Reading the M3 eval numbers correctly

Baseline is **precision 0.2667, recall 1.0000** at `top_k=5`. The precision looks alarming and
is not. Most golden queries have exactly **one** expected memory, so precision@5 is
mathematically capped at **0.2** for those — 1 relevant document out of 5 returned. 0.2667 is
therefore *at or above* the ceiling, and recall 1.0 means every single target was retrieved.

This is a property of the metric definition, not of retrieval quality. Flagged because M8's
regression gate compares against this file: the ceiling applies identically to v2, so the
comparison stays valid — but nobody should later read "precision 0.27" as a defect and "fix"
it.

#### M3 verification: **FAIL** — and this is the case for having verifiers at all

All 9 DoD lines passed. All 12 named tests passed. The milestone still failed, because the
verifier asked the one question the tests do not: *is the thing this test proves actually
true?*

**The blocker.** The plan's keyword-only probe requires a query matchable *only* via keyword.
The builder's design was `organ` → the "organic vegetables" row, via a stemming collision.
The collision is real and the verifier confirmed it independently. The **separation** is not.

Ranking the entire 31-row corpus by cosine — no `LIMIT` to hide behind — puts the target at:

```
1. commute 0.58403    4. coffee  0.61144
2. openmic 0.60323    5. dentist 0.61235
3. strings 0.61002    6. veg     0.61364   <== TARGET
```

Rank **6 of 31**, missing the `SEMANTIC_TOP_K=5` cut by **0.00128 cosine — 0.21%**. That is a
rank-5/6 tiebreak, not a semantic separation. Two experiments settled it:

- **Cold query cache:** re-embedding `organ` live instead of using the cached vector flips the
  ordering — `veg` moves to rank 5, *inside* top-k, and the test **fails** with
  `target was contributed by ['keyword','semantic']`.
- **`SEMANTIC_TOP_K=6`**, a plausible M4/M5 tuning change: same failure.

The decisive measurement: Voyage is not bit-deterministic — `max |elementwise diff| = 1.06e-03`
between two calls on the *same text*. That noise is the **same order of magnitude as the
0.00128 margin**. So the test was green because of one cached float array, and any re-embed,
model revision, top-k change, or corpus edit was a coin flip on it.

**Why this had to be caught now.** M8 gates against this milestone's baseline. A probe that
passes on a warm cache and fails on a cold one would have surfaced three milestones later as
an inexplicable M8 regression, with the actual cause buried in a fixture file.

**What the verifier confirmed as genuinely sound**, so the rework stays narrow: concurrency is
a real `asyncio.gather` (0.313s vs 0.513s sequential — a sequential implementation would fail
the test's own ceiling); per-path isolation and timeouts work against the live DB (forced
failure and a 30s hang at `PATH_TIMEOUT_MS=200` both returned 5 semantic candidates with the
degradation logged); the merge is a true union keyed on `memory_id`; both paths filter
`deleted_at IS NULL`; no graph search; and the cached vectors are genuine Voyage output
(unit-norm, `cosine(live, cached) = 0.99996`, negative control 0.286).

The verifier also went past the brief and found that **RLS alone already blocks the
cross-subject leak** — removing the app-level `subject_id` predicate from the semantic SQL
still returned only subject A's row. So `test_retrieval_scoped_to_subject_id` passes, but the
application-layer predicate it appears to test is defence-in-depth, not the thing doing the
work. Worth knowing before M7 builds on it.

##### Three further defects sent back with the blocker

| # | Defect | Severity | Why it matters |
| --- | --- | --- | --- |
| 2 | `run_eval.py` prints `PATH EXPECTATIONS NOT MET` and still **exits 0** | should-fix | The DoD command and any M8 CI wiring see a clean pass on a broken golden set. Only the pytest wrapper caught it, by string-matching stdout. |
| 3 | `macro_precision` reduces exactly to `mean(\|expected_i\|)/5` — a function of label counts, not the retriever | should-fix | Breaks M8's gate in **both** directions: `recall >= 1.0` demands perfection, and adding v2 queries with one expected doc each drags precision down and fails the gate *even if retrieval improved*. Fix is additive — add MRR and precision@\|expected\| without touching the existing keys M8 reads. |
| 4 | A singleton path min-maxes to 1.0, so a lone keyword hit with `ts_rank` 0.06 ties the best semantic match | should-fix | M4 weights semantic score at 0.4, so an inflated path score distorts ranking downstream. |

Verifier was scrupulous about what it could *not* confirm: the builder's "four failed designs"
narrative (no history in the tree — only the current design exists), the `get_pool()` race
(correctly declined to probe a file M2 was editing), and the new 429 backoff (never fired
across its 3 live calls).

**Status: 🔴 sent back to the builder** with the blocker plus defects 2–4, and an explicit
standard for the fix: rank the whole corpus with no LIMIT, prove the new target holds on a
**cold** cache and at `SEMANTIC_TOP_K=10`. A green test on a warm cache is not evidence.

#### M3 rework — the fix, and a better finding than the fix

**The old design was abandoned, not patched, and the measurement says why.** The builder first
tried the obvious repair: seed 12 rows genuinely about transplants, anatomy and church
instruments to push the `veg` target down the `organ` ranking. It **failed** — `veg` stayed at
rank 6 while `commute` ("I cycle to the office") ranked *first* for `organ`, and the new
medical rows didn't even reach the top 22.

The explanation is worth keeping: **a bare single-word query embeds near-uniformly.** The whole
43-row corpus sat inside a 0.58–0.72 cosine band, so the ordering carried no steerable signal
at all. `veg` had been floating high only because "organic" is morphologically close to
"organ" — not because of meaning. You cannot fix a boundary case by adding distractors when
the ranking itself is noise.

So it screened all 12 Snowball stemming collisions with distant surface forms in **one batched
request** (pure vector math against the cached corpus — the right way to work under a
3-req/min quota):

| query | carrier rank | margin@10 | | query | carrier rank | margin@10 |
| --- | --- | --- | --- | --- | --- | --- |
| mine | 1/43 | −0.069 | | logic | 21/43 | +0.021 |
| plate | 1/43 | −0.108 | | busy | 22/43 | +0.062 |
| sole | 2/43 | −0.039 | | moral | 25/43 | +0.039 |
| organ | 9/43 | −0.010 | | critic | 27/43 | +0.053 |
| mental | 9/43 | −0.008 | | **origin** | **30/43** | **+0.098** |
| major | 16/43 | +0.032 | | minor | 32/43 | +0.090 |

**Adopted: `gs-002 = "origin"` → the `tiles` row** ("The original tiles in the hallway are
cracked beyond repair"). Both stem to `'origin'`; the embedder sees cracked hallway tiles and
reads the query as provenance.

| | target rank | margin@5 | margin@10 | keyword@500 |
| --- | --- | --- | --- | --- |
| warm cache | **31 / 44** | +0.13039 | +0.10306 | `['tiles']` |
| **cold cache, live re-embed** | **31 / 44** | +0.13017 | +0.10272 | `['tiles']` |

The two runs differ by ~2e-4. The margin is **~100× Voyage's 1e-3 drift** and ~500× the old
design's 0.00128. That is the difference between a probe and a coin flip.

**A drift bug the builder found while proving the cold cache** — and reported rather than
quietly fixing: its first cold run failed with `{'semantic': 'timeout after 5000ms'}`, which
*looks* like a separation failure and is not. The test module hardcoded
`QUERY_KEYWORD_ONLY = "organ"`, so once the golden query changed, the warm-up fixture primed a
query no test used and the real one was embedded live **inside** `asyncio.wait_for`. Invisible
on a shared warm cache. Test queries are now read from `golden_set.jsonl` so they cannot drift
apart from the fixtures again.

**Normalization turned out to be load-bearing.** Min-max was corpus-relative — it promoted
whichever result was best to 1.0 *however weak it was*. Replaced with fixed absolute reference
scales (semantic = clamped cosine; keyword = `rank/(rank + 0.0607927)`, the constant verified
in SQL as the `ts_rank` of a single-term match, so that canonical case scores exactly 0.5).
`tiles` now ranks first outright instead of tying `sister` on a UUID sort.

##### The finding that matters more than the fix: the baseline is saturated

Adding MRR and precision@R revealed that **both come out at 1.0000**. So `recall`, `MRR` and
`P@R` are *all* at ceiling on the v1 golden set. Every headline metric can only regress, never
improve.

That makes M8's `test_eval_v2_meets_or_exceeds_v1_baseline` a **tripwire, not a discriminator**
— it can catch a catastrophic regression but cannot show that retrieval got better, which is
what the plan's M8 narrative implies it does. Combined with the earlier precision finding, the
v1 baseline has three metrics pinned at 1.0 and one pinned at `mean(|expected|)/k`.

The runner now prints a `SATURATED` warning stating that v2 needs genuinely harder queries
(near-miss distractors, multi-hop phrasing) if the baseline is to discriminate.

#### M3 re-verification: **PASS**

The independent verifier reproduced the fix rather than accepting it:

| Condition | `tiles` rank | margin@5 |
| --- | --- | --- |
| Warm cache, whole corpus, no LIMIT | 31 / 44 | +0.13039 |
| **Live re-embed**, whole corpus, no LIMIT | 31 / 44 | +0.13063 |

Delta warm-vs-live **2.4e-4** against a 0.13 margin — ~77× the observed re-embed drift. It also
located the crossover: `SEMANTIC_TOP_K=31` is the *smallest* value that trips the path
expectation, so the probe survives k=5/10/20/30. Last round a live re-embed moved the target
across the cut; now it moves it by 2e-4. That is the whole difference.

It independently re-ran the abandoned design too and confirmed every specific: `commute`
("I cycle to the office") really does rank **first** for `organ`, `veg` really stayed at rank 6,
and the 12 seeded anatomy rows landed at ranks 27–38. The builder's reasoning was sound.

One honest caution the verifier raised and I am recording rather than burying: `origin` is
*also* a bare single-word query, so by the builder's own argument its ordering is near-arbitrary
too. The draw simply landed with a 0.13 margin instead of 0.0013, verified across two
independent embeddings. **That is luck with a large safety factor, not a designed mechanism** —
which is exactly why defect 1 below (assert the margin) matters.

##### Independent judgement on saturation — confirmed, and it rewrites M8's gate

The verifier recomputed per-query rather than trusting the aggregate: **all 9 queries have
`reciprocal_rank = 1.0` and `precision_at_r = 1.0`.** Not near ceiling — exactly at it,
unanimously. Its verdict: M8's `test_eval_v2_meets_or_exceeds_v1_baseline` **as the plan
specifies it is unfit for purpose**, for three separate reasons:

1. `recall >= 1.0` demands perfection. The plan asks M8 to *expand* the golden set **and**
   meet-or-exceed v1 — instructions in direct conflict, since any query hard enough to be worth
   adding risks a miss. The gate would punish M8 for doing what the plan asks.
2. `precision >= 0.266667` is gameable in **both** directions: adding single-answer queries
   fails it with a perfect retriever, while labelling more documents per query passes it without
   touching retrieval. A metric you pass by relabelling is not a regression gate.
3. MRR and P@R are the right metrics but at 1.0 inherit the same can-only-regress property.
   They will discriminate on a harder v2; they cannot serve as a baseline drawn from v1.

**Binding guidance for the M8 brief:**

- Gate on the **v1 queries as a held-out subset inside v2**, at `recall = MRR = P@R = 1.0`. An
  honest no-regression tripwire — and running the same 9 queries against v2's larger corpus is
  itself a genuinely harder condition.
- Report v2's **new-query metrics as characterization, not a gate**, on first run; they become
  the baseline for whatever follows.
- **Drop macro precision@5 from the gate**, keeping the key in the JSON for compatibility.
- v2 must be **harder, not merely bigger**: near-miss distractors, multi-hop phrasing, queries
  whose answer is deliberately not at rank 1. A 44-row corpus where every query hits at rank 1
  is below the resolution of any metric.

##### Two defects sent back after the PASS

| # | Defect | Why it matters |
| --- | --- | --- |
| 1 | **Nothing asserts the margin the design rests on.** The test asserts only set membership at one k; `run_eval` prints no rank or margin. | A future corpus edit eroding +0.13039 to +0.002 leaves **every test green** while the probe reverts to the exact coin flip this milestone was failed for. The guard must assert the property, not a boolean. |
| 2 | `evals/metrics.py:161-177` — MRR/P@R computed on the **untruncated** list while precision/recall use top-k truncation. | Invisible here (every hit is rank 1). On a harder v2 a hit at rank 8 with k=5 reports `recall@5 = 0.0` beside `MRR = 0.125` — one metric says "missed", the other "found". M8 reads these keys. |

Plus cosmetics: a 164 KB unreferenced `_screen_queries.json` that would be committed, a stale
docstring still naming `organ`/`veg` (the same class of stale reference that caused the drift
bug), and query-cache entries never pruned.

The verifier also caught the builder **overclaiming** in a comment — `seed_memories.py:303`
says the rank/margin numbers are "re-derived by run_eval on every run and asserted by the
integration test". They are not; the verifier derived them by hand. Sent back to be made either
accurate or true.

#### M2 verification: **PASS**

All 7 DoD lines, all 12 named tests, no skips, no xfails. `30 passed in 315.34s`.

**The control experiment is the model for how these should be verified.** The likeliest way to
fake `test_concurrent_identical_turns_do_not_double_write` is a read-then-write race that
happens to pass. So the verifier neutralised the advisory lock, changed nothing else, and ran
both versions five times each:

| Configuration | Rows | Actions |
| --- | --- | --- |
| Lock as shipped | **1 row in 5/5 trials** | `['insert', 'reinforce']` |
| Lock neutralised | **2 rows in 5/5 trials** | `['insert', 'insert']` |

The test genuinely fails without the mechanism. It also confirmed the subtle prerequisite:
`session()` wraps in `conn.transaction()` (`store/db.py:216`), so `pg_advisory_xact_lock` is
really transaction-scoped rather than a silent no-op under autocommit — which is exactly where
this would have quietly not worked.

Other checks that went beyond the plan:

- **PII scanned table-wide, not the 5 rows the DoD asks for.** All 45 rows: `ssn_like 0`,
  `nine_digit_run 0`, `email_like 0`, `at_sign 0`, `card_like 0`. Read back **as superuser** so
  RLS could not hide a leak. Confirmed `pii.py` **fails closed** — returns
  `[REDACTED_UNSCANNABLE]` if Presidio errors, rather than passing raw text through.
- **Dedup scoping proven independent of RLS.** This is the exact gap the M3 verifier found on
  the retrieval side. On a superuser connection with both subjects' rows visible,
  `find_similar(A)` and `find_similar(B)` each returned only their own row — so the app-level
  predicate is genuinely doing the work here, unlike in `retrieve/semantic.py`.
- **Node order confirmed at runtime**, not just from the compiled graph, via live Prometheus
  counters: `extract 1→2, pii 2→2, evaluate 2→2, embed 2→2, dedup 2→2, write 2→2`.
- **Off-request-path proven twice**: grep shows no `await` on any enqueue path, and a live turn
  measured a **9.6s gap** between reply and row appearing.

##### Defects sent back after the PASS

| # | Defect | Why it matters |
| --- | --- | --- |
| 1 | **Structured logging is inert.** `memsys.capture` logger has no handlers and effective level WARNING, so every `log_event()` is discarded; the server log had zero capture lines. Plan step 13's Prometheus half works fully. | Step 13 is half-delivered, and the plan's DoD line 2 refers to "the test's output/**logs**" — currently satisfied only because the test asserts directly. |
| 2 | `test_extract_returns_empty_for_nonmemorable_turn` **can pass vacuously** — `extract_candidates()` swallows provider errors and returns `[]`, so a dead or throttled Groq reports green. | Acute on this project specifically, since the provider *is* rate-limited. Needs to assert no `provider_error` occurred. |
| 3 | **The worker pool's concurrency is never tested end-to-end.** The concurrency test calls `run_capture` directly, bypassing the queue — but `CAPTURE_WORKER_CONCURRENCY=2` is the production path that makes the lock necessary. | The lock is proven; the thing that exercises it is not. |

Cosmetics noted: an exhausted embed retry returns `[]` and short-circuits the graph, making a
quota failure look like "nothing to embed"; and extraction over-produces overlapping facts
(one live turn wrote both *"The user plays the cello"* and *"The user plays the cello on Sunday
mornings at the community hall"* — similarity below the 0.82 threshold, so two rows). The
second is within plan but is a memory-quality note for M4.

### D10 — The long-running dev server goes stale, and it has now cost two verifications
Both the M1 and M2 verifiers hit a `:8000` process serving **older code than the working tree**.
For M1 it was a 46-second-old binary; for M2 the server predated the chat router entirely and
returned **404 on `/chat`**, which would make anyone following the DoD literally conclude the
milestone had failed.

This is an orchestration miss, not an agent error — I restarted the server after M1 and then
let it go stale again while M2 added routes. Restarted again; `/health` and `/chat` both 200.
Two stale python processes were found running, so the earlier restart had also leaked one.

Standing rule from here: **restart the API before dispatching any verifier**, and prefer
in-process ASGI clients in tests over the long-running server. Recorded in kickoff.md's
gotchas so it does not have to be rediscovered a third time.

### D9 — Rate limits are now the dominant constraint, not correctness
Both W2 agents have been killed twice by session rate limits mid-task. Both times I surveyed
the on-disk state first and **resumed** rather than cold-restarting, so each agent kept its
reasoning; a restart would have re-derived everything at full cost and risked one agent
overwriting the other's files.

Related and worth separating: M2's `test_distinct_facts_create_separate_rows` "hang" I flagged
earlier was **not** a deadlock — it was 69s of my own Voyage backoff doing its job. Diagnosed
by the agent, not by me. The lesson is that after adding backoff, "slow" and "hung" stop being
distinguishable by observation alone, so bounded per-test timeouts are now required rather
than optional.

Builder-reported extras worth keeping:

- Two tests beyond the plan's 11: a `max_tokens >= 512` floor guard, and a tree-wide grep
  that fails if a provider-prefixed model literal appears outside `llm/config.py`. The
  second is the real enforcement of plan step 14 — the plan's own
  `test_llm_config_reads_model_from_env` monkeypatches env and so cannot catch a hardcoded
  literal sitting in a *caller*. Good catch by the builder.

---

## Decision log (continued)

### D6 — Host ports 5432/6379 were NOT free; containers remapped
Discovered by the M1 builder, not by my pre-flight probe — my port check was an assumption
carried over from the plan's prerequisite list, and it was wrong. Recording that as a
process miss, not just a fact.

- A native **PostgreSQL 18** Windows service holds `5432`; native **Memurai** holds `6379`.
  Neither stops without an elevated shell.
- Containers remapped to host **55432** / **56379** via `${POSTGRES_HOST_PORT}` /
  `${REDIS_HOST_PORT}`, so reverting is a one-line env change once the native services are
  stopped.
- **Why this mattered more than a port number:** had the builder silently mapped the
  container onto 5432, every probe in `demo_m1.sh` would have connected to the *native*
  PostgreSQL and passed — a fully green milestone sitting on the wrong database. This is
  exactly the failure the plan's DoD line 3 was written to catch. The builder flagged it
  instead of papering over it, which is the behaviour I want.
- The verifier is specifically tasked with proving the probes hit the **containerized**
  services, since a green demo script is not by itself evidence of that.
- Plan prerequisite "Ports … are free" is deliberately left **unticked** with the reason
  inline, rather than ticked as if it were satisfied.

### D7 — Two Windows-specific traps found during M1
Both are environment landmines that would have cost time in every later milestone:

1. **psycopg async on Windows.** The default `ProactorEventLoop` cannot run psycopg's async
   driver — `/health` reported `postgres:false` until fixed.
   `store.db.ensure_selector_event_loop_policy()` now runs from `conftest.py` and
   `api/main.py`. Consequence for everyone: **start the API with `python -m api.main`, not
   `uvicorn api.main:app`** — uvicorn's CLI builds the wrong loop before the app is imported.
2. **`pg_policies.qual` is NULL for INSERT policies.** PostgreSQL forbids `USING` on INSERT;
   the predicate lives in `with_check`. The plan's DoD line reads `select tablename, qual`
   and expects both columns named in every row, which INSERT can never satisfy. Correct
   check is `coalesce(qual, with_check)`. This is a defect in the *plan's* verification
   command, not in the implementation — flagged for the user rather than silently "fixed",
   since only the user edits their own DoD.
