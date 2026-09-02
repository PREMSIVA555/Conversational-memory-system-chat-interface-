# kickoff.md — memory-system milestone progress

Persistent conversational-memory layer behind an LLM chat agent. Single-user, self-service.

- **Spec:** [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — the source of truth. Not edited except to tick boxes.
- **Orchestration log:** [harness.md](harness.md) — decisions, waves, dispatch records.

---

## Status legend

| Symbol | Meaning |
| --- | --- |
| ⬜ | Not started |
| 🔨 | Builder agent working |
| 📋 | Builder claims done — awaiting independent verification |
| ✅ | **Verified** by a separate agent that did not write the code |
| 🔴 | Verification failed — sent back to the builder |
| 🏁 | Signed off by the user in IMPLEMENTATION_PLAN.md |

`✅` is the highest state I can grant. `🏁` is yours alone — it means you ran the
Definition of Done commands yourself and saw the expected output with your own eyes.

---

## Progress

| # | Milestone | Wave | Status | Verified by |
| --- | --- | --- | --- | --- |
| M1 | Docker infra, core tables (HNSW+GIN+RLS), `/health`, LiteLLM wrapper | W1 | ✅ | independent agent — 12/12 DoD, 13/13 tests |
| M2 | Capture graph: extract → PII → evaluate → embed → dedup → write | W2 | ✅ | independent agent — 7/7 DoD, 12/12 tests, lock proven by control experiment |
| M3 | Hybrid retrieval (pgvector ∥ tsvector) + golden-set eval harness | W2 | ✅ | independent agent — **failed once**, reworked, passed on re-verification |
| M2.5 | *(inserted)* Thin chat UI — first visible demo | W3 | ✅ | **failed once** (reply outside the aria-live region), fixed, re-checked |
| M4 | Weighted ranking + token-bounded context composer | W3 | ✅ | independent agent — 7/7 DoD, 12/12 tests; 4 defects closed |
| M5 | Streaming response graph + Redis circuit breaker | W4 | ✅ | **failed once** (dead `NOSCRIPT` fallback), fixed, passed re-verification |
| M7 | Governance: audit log, curated view, soft-delete, GDPR export | W5 | ✅ | independent agent — 9/9 DoD, 15 tests; 5 defects closed |
| M6 | Next.js real-time chat UI + memory management panel | W6 | ⬜ | — |
| M8 | Distributed decay job, reflection agent, evals vs. M3 baseline | W6 | 📋 | **failed cold verification 8/10**, two blockers fixed, awaiting re-verification |

*Rows are ordered by execution wave, not by milestone number — M2.5 and M4 run before M5,
and M7 runs before M6 so the memory panel wires real endpoints instead of mocks.*

### M8's cold verification: 8 of 10, and what it caught

A verifier that did not write any of this re-ran all ten Definition of Done lines and
**failed the milestone**. It was right to. The two failures were the two lines that were
supposed to *prove* M8 rather than describe it, and both had been ticked.

**DoD 6 — the gate had never actually run.** The DoD's command is
`python evals/run_eval.py --suite golden_set_v2`, with no `--baseline`. `--baseline`
defaulted to `None`, so that command printed no gate at all — while the same DoD line
requires it to print "the explicit delta and pass/fail against the baseline". The committed
`golden_set_v2.json` proved it: `"gate": {"baseline": null, "passed": null, "rows": []}`.
The milestone's own recorded artifact showed the gate had never been exercised. **Fixed** —
a suite with a baseline is now gated by default (`--no-baseline` opts out), because a gate
you have to remember to switch on is not a gate.

**DoD 9 — the reflection job wrote nothing 7 times in 9, and exited 0 every time.** The
verifier isolated the root cause rather than calling it flaky:

```
finish_reason=length  content=''  completion_tokens=1024  reasoning_tokens=1022
```

`gpt-oss` spends its budget on internal reasoning *before* emitting content — a trap
`llm/config.py` already documents — and the global 1024-token default left **two tokens**
for the answer on a prompt that asks the model to find a shared theme across eight
memories. **Fixed** on both axes: the summary call gets its own `SUMMARY_MAX_TOKENS = 3072`
(0/6 empty on the exact cluster that previously failed, was 3/9), and a run that finds a
cluster and writes nothing now **raises instead of exiting 0**. Having nothing to
consolidate still exits 0 — a cron that alarms on a genuinely quiet night gets muted, and a
muted alarm is worse than the silence it replaced.

Two findings I have **not** closed, because they are yours to decide:

- **DoD 6 cannot pass as literally written**, and is now unticked in the plan with the
  reason inline. v2's blended recall is 0.9763 against v1's 1.0 — because v2 adds harder
  queries *on purpose*. It passes only on the nine v1 queries held out inside v2. That gate
  is defensible and documented, but it is a reinterpretation of the line, not a reading of
  it. Same class as M1's `pg_policies` defect: flagged, not silently rewritten.
- **DoD 9 assumes an unconsolidated corpus.** Run it twice and the second run correctly
  reports `no_cluster` and writes nothing. That is right behaviour and a wrong DoD line.

Also worth keeping from the verifier, unfixed and honest: `test_the_sweep_was_actually_
concurrent` asserts `>= 2` workers, so it would still pass a 299/1/0 split. The observed
runs (269/15/16, then 269/16/15) do establish three real concurrent processes — but the
*test* does not enforce what DoD line 2 claims; a human reading the output does.

### Why M8 is `📋` and not `✅`

M8 was built in two halves by two different hands, and only one of them has ever been
graded by someone who did not write it.

The **decay and reflection half** (plan steps 1–12, 16) came from a builder agent whose
session ended before it ticked a box or logged anything — the work was found sitting
untracked in the working tree. The **evals half** (steps 13–15 and the two gate tests) was
written by the orchestrating session itself, because the remaining work was small and
tightly specified and a fresh builder would have paid full cold-start cost to re-derive
context that was already loaded.

That trade is defensible for the *building*. It is not defensible for the *grading*: the
rule this project runs on is that an agent grading its own homework rationalizes a partial
pass, and for the evals half I am now that agent. Everything below was re-run and is
reported honestly, but **`✅` requires a cold verifier and M8 has not had one.**

What a verifier should be told to distrust first:

1. **The 270/15/15 worker split.** `pytest tests/distributed/` passes and three real pids
   genuinely run, but `SKIP LOCKED` guarantees no fairness. The same green would appear if
   two of the three workers had barely participated. Read the per-worker counts.
2. **The gate is a tripwire, not a discriminator, by design.** It compares v1's nine
   held-out queries against a baseline where recall = MRR = P@R are all exactly 1.0, so it
   can only ever catch a regression. That is deliberate (harness.md records why a
   `v2_blended >= v1` gate would have been incoherent), but it means a *passing gate is
   weak evidence* — the strong evidence is the `v2_new` tier being unsaturated.
3. **The reflection job exits 0 having written nothing.** See the open item below. The DoD
   line passes, but it passed on a retry.

---

## The stack, as resolved

| Concern | Choice | Why |
| --- | --- | --- |
| Completions | Groq `openai/gpt-oss-120b` | User's key; verified live. Fallback `qwen/qwen3.8-27b`. |
| Embeddings | Voyage `voyage-3.5`, 1024-dim | Groq has no embeddings endpoint. Retrieval-tuned. |
| Vector store | Postgres 16 + pgvector, HNSW cosine | Plan-specified. One datastore for vector + keyword. |
| Keyword search | Postgres `tsvector` + GIN | Runs in the same DB, so hybrid merge needs no cross-store join. |
| Isolation | Row-level security on `subject_id` **and** `actor_id` | Forward-compatible seam per plan step M1.8. |
| Orchestration | LangGraph | Plan-specified. |
| Frontend | Next.js App Router + TypeScript | Plan-specified. |

---

## How a milestone actually completes

1. A **builder** agent implements the milestone's steps and tests, ticking its boxes in the plan.
2. Builder reports done → status `📋`. **This is a claim, not completion.**
3. A **separate verifier** agent — cold, no memory of writing the code — re-runs every line of
   that milestone's *Definition of Done* and reports pass/fail per line.
4. Pass → `✅`. Fail → `🔴`, findings go back to the builder, repeat.
5. You run the same commands yourself and fill in the sign-off line → `🏁`.

The builder/verifier split exists because an agent grading its own homework will
rationalize a partial pass. The verifier is told to trust nothing it did not run.

---

## Run it yourself

```bash
cp infra/.env.example infra/.env      # then paste your GROQ_API_KEY and VOYAGE_API_KEY
bash scripts/dev_up.sh                # compose up + migrations
bash scripts/demo_m1.sh               # exits 0 when the foundation is sound

# M8: the maintenance jobs and the regression gate
pytest tests/distributed/test_decay_claims_no_double_process.py   # 3 real worker processes
python evals/run_eval.py --suite golden_set_v2 \
       --baseline evals/results/golden_set_v1.json                # exit 3 = retrieval regressed
python -m jobs.run --job decay                                    # weights visibly drop
python -m jobs.run --job reflection                               # writes one summary memory
```

Start the API with **`python -m api.main`**, not `uvicorn api.main:app` — see the port and
event-loop notes below.

*(Commands become real as milestones land; this block is kept accurate wave by wave.)*

---

## Things that will bite you on this machine

Two environment landmines found during M1. Both are recorded in detail in
[harness.md](harness.md) (decisions D6, D7).

**1. Ports 5432 and 6379 are already taken.** A native PostgreSQL 18 service and a native
Memurai service hold them, and neither stops without an elevated shell. The containers are
therefore on **55432** and **56379**, wired through `POSTGRES_HOST_PORT` / `REDIS_HOST_PORT`
so it's a one-line revert once those services are stopped.

This is worth more than a footnote. Had the containers been mapped onto 5432 anyway, every
health probe would have connected to your **native** PostgreSQL and reported green — a fully
passing milestone built on the wrong database. The plan's DoD line about probing container
ports exists to catch exactly that, and it earned its place here.

**2. psycopg's async driver cannot run on Windows' default event loop.** `/health` reports
`postgres:false` until the selector policy is set. Handled in `store/db.py`, but it means
`uvicorn api.main:app` breaks the app while `python -m api.main` works — uvicorn's CLI
builds the loop before the app is imported.

**3. The long-running dev server on :8000 goes stale, and it has already cost two
verifications.** It keeps serving whatever code was on disk when it started. For M1 that was a
46-second-old build; for M2 the server predated the chat router entirely and returned **404 on
`/chat`** — anyone following that milestone's Definition of Done literally would have concluded
M2 had failed. **Restart it after any code change** (`python -m api.main`), and prefer
in-process ASGI test clients over the long-running server. Stale processes also leak: two were
found running at once.

**4. The Voyage key is rate-limited to 3 requests/minute** ("no payment method on file"). This
is the single biggest drag on iteration — the M2 suite takes 6–14 minutes, almost all of it in
backoff. `llm/config.py` retries 429s so correctness is unaffected, but two consequences
follow: batching embeds is a **quota** requirement rather than an efficiency nicety, and "slow"
is no longer distinguishable from "hung" by observation, so every test needs a bounded timeout.

**5. LiteLLM memoises a global async HTTP client bound to one event loop.** pytest-asyncio
builds a fresh loop per test, so the second test to call a provider inherits a dead client and
fails with `Event loop is closed` (surfacing as `APIConnectionError`). Handled by an autouse
flush fixture in the repo-root `conftest.py`. **This looks exactly like a rate limit** — both
present as "fails in a full run, passes alone". The traceback is what tells them apart.

---

## A defect in the plan itself

Plan M1, Definition of Done, the `pg_policies` line:

```
psql "$DATABASE_URL" -c "select tablename, qual from pg_policies where tablename='memories'"
```

This can never pass as written. PostgreSQL forbids `USING` on an INSERT policy — the
predicate lives in `with_check`, so `qual` is `NULL` for that row no matter how correct the
policy is. The check that actually tests what you meant is:

```sql
select tablename, policyname, coalesce(qual, with_check) from pg_policies where tablename='memories';
```

Flagged rather than silently corrected — the Definition of Done is yours, and I'd rather you
change it knowingly than find your verification command quietly rewritten.

---

## Open items

- [x] ~~**The reflection job exits 0 when it writes nothing.**~~ **Closed.** Cold
      verification measured it far worse than the first report: 7 runs in 9 wrote nothing,
      all exiting 0. Root cause was not "the model is flaky" but a measured budget
      exhaustion — `finish_reason=length`, 1022 of 1024 tokens spent on reasoning, two left
      for the answer, on a prompt asking for a shared theme across eight memories. Fixed
      with a dedicated `SUMMARY_MAX_TOKENS = 3072` (0/6 empty on the previously-failing
      cluster) **and** a `ReflectionProducedNothing` raise so a run that finds work and does
      none of it exits non-zero. Four regression tests guard both halves.

- [ ] **Two DoD lines need your decision, not more code.** Both are verification-command
      defects in the same class as M1's `pg_policies` line, so they are flagged rather than
      silently reinterpreted:

      - **M8 DoD 6** asks that "v2 precision and recall are each at or above the M3
        baseline". v2's blended recall is 0.9763 against v1's 1.0 — because v2 adds harder
        queries, which the same plan step asks for. The two halves of the plan contradict.
        The implemented gate compares the nine v1 queries held out inside v2, which is a
        real no-regression test against a corpus half again as large. Amend the line to say
        so, or tell me to gate differently. It is currently **unticked** with the reason
        inline in the plan.
      - **M8 DoD 9** ("run the reflection job → a new row appears") assumes an
        unconsolidated corpus. A second consecutive run correctly finds nothing left to
        consolidate and writes nothing. Right behaviour, wrong DoD line.

- [ ] **`pytest tests/` could not collect at all until this session.**
      `tests/unit/test_decay.py` and `tests/integration/test_decay.py` share a basename, and
      under pytest's default `prepend` import mode that aborts collection of the whole tree
      with `import file mismatch`. Fixed by adding `--import-mode=importlib` to `addopts`.

      Worth noting as a process point rather than a bug: M8's own final DoD line is
      `pytest tests/ -v`, so this had been broken since the moment those two files were
      written, and only surfaced when someone ran that line. Every suite passed
      individually the whole time.

- [ ] **Rotate the Voyage key now, not at project end.** Beyond having been pasted into a chat
      transcript, LiteLLM was observed dumping request headers — including the key in
      plaintext — into a failing traceback during M2. Anywhere those logs were written or
      copied has the key in clear text. Rotate the Groq key too, on the transcript grounds.
- [ ] **Add a payment method to the Voyage account — this is now the highest-impact fix
      available, and it is no longer just about speed.**

      The key is throttled to **3 requests/minute** ("no payment method on file"). Measured
      single-query embedding latency on the live path:

      | query | latency |
      | --- | --- |
      | "what do you know about my family?" | **12.51s** |
      | "what am I allergic to?" | 0.30s |
      | "tell me about my hobbies" | 0.36s |
      | "what is my commute like?" | **63.75s** |

      Within-quota calls take ~0.3s. Once the three-per-minute window is spent, the retry
      layer backs off and a single embed takes 12–64s. **`PATH_TIMEOUT_MS` is 5000ms**, so
      the semantic path times out and the turn is served without memory. A live chat request
      just now returned `x-memory-degraded: true`, `reason: timeout`.

      Capture embeds too, so a normal conversation exhausts the window quickly and the
      **retrieval path degrades most of the time**. Nothing is broken — M5's breaker is doing
      precisely its job, which is why the reply still returns fast — but the demo will show
      "answering without memory" far more often than it shows memory working.

      Do **not** fix this by raising `RETRIEVAL_TIMEOUT_MS`: that trades a fast degraded
      reply for a slow one and makes the chat feel broken. The quota is the actual
      constraint.
- [ ] Decide whether to stop the native PostgreSQL 18 / Memurai services and reclaim the
      standard ports, or keep the 55432 / 56379 mapping.
- [ ] Consider amending the plan's `pg_policies` DoD line to use `coalesce(qual, with_check)`.
- [ ] User sign-off pending on every milestone.
