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
| M1 | Docker infra, core tables (HNSW+GIN+RLS), `/health`, LiteLLM wrapper | W1 | 📋 | verifier running |
| M2 | Capture graph: extract → PII → evaluate → embed → dedup → write | W2 | ⬜ | — |
| M3 | Hybrid retrieval (pgvector ∥ tsvector) + golden-set eval harness | W2 | ⬜ | — |
| M2.5 | *(inserted)* Thin chat UI — first visible demo | W3 | ⬜ | — |
| M4 | Weighted ranking + token-bounded context composer | W3 | ⬜ | — |
| M5 | Streaming response graph + Redis circuit breaker | W4 | ⬜ | — |
| M7 | Governance: audit log, curated view, soft-delete, GDPR export | W5 | ⬜ | — |
| M6 | Next.js real-time chat UI + memory management panel | W6 | ⬜ | — |
| M8 | Distributed decay job, reflection agent, evals vs. M3 baseline | W6 | ⬜ | — |

*Rows are ordered by execution wave, not by milestone number — M2.5 and M4 run before M5,
and M7 runs before M6 so the memory panel wires real endpoints instead of mocks.*

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

- [ ] **Rotate the Groq and Voyage keys at project end** — both were pasted into a chat
      transcript, so treat them as disclosed.
- [ ] Decide whether to stop the native PostgreSQL 18 / Memurai services and reclaim the
      standard ports, or keep the 55432 / 56379 mapping.
- [ ] Consider amending the plan's `pg_policies` DoD line to use `coalesce(qual, with_check)`.
- [ ] User sign-off pending on every milestone.
