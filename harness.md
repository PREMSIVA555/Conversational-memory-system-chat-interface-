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
- **Verifier:** dispatched, briefed to attack 8 specific claims. Status: in progress.
- **Status:** 📋 claimed — NOT counted until the verifier reports.

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
