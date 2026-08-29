# memory-system

A persistent conversational-memory layer that sits behind an LLM chat agent — it
remembers facts about one person across sessions, retrieves the relevant ones,
and fits them into a bounded prompt block.

Built milestone by milestone against [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md),
which is the single source of truth for scope, tests, and sign-off.

**Current state: M1 complete** — container stack, schema with HNSW + GIN + RLS,
FastAPI `/health` and `/metrics`, and the LiteLLM provider seam.

---

## Boot sequence

Everything below assumes a shell at the repo root. `scripts/*.sh` are POSIX and
run under Git Bash on Windows.

### 0. One-time setup

```bash
py -3.11 -m venv .venv                     # Python 3.11 — several deps lack 3.14 wheels
.venv/Scripts/python -m pip install -e ".[dev]"

cp infra/.env.example infra/.env           # then fill in the values
```

`infra/.env` is gitignored and is the only place real secrets live. You need a
`GROQ_API_KEY` (completions) and a `VOYAGE_API_KEY` (embeddings) — see
[harness.md](harness.md) D1/D2 for why the providers are split.

### 1. Containers

```bash
docker compose -f infra/docker-compose.yml up -d
docker compose -f infra/docker-compose.yml ps      # all five must read (healthy)
```

### 2. Migrations

```bash
.venv/Scripts/python -m store.migrate
.venv/Scripts/python -m store.migrate --status     # what ran, and how many times
```

Safe to run repeatedly — every file is individually idempotent and the runner
re-applies all of them on every invocation (see `store/migrate.py` for why it
does not skip).

### 3. API

```bash
.venv/Scripts/python -m api.main
curl -sf localhost:8000/health
```

Start it with `python -m api.main`, **not** `uvicorn api.main:app`. The module's
`__main__` block installs the Windows selector event-loop policy that psycopg's
async driver requires; `uvicorn` invoked directly creates a ProactorEventLoop
first and every Postgres call then fails.

### Shortcut

```bash
bash scripts/dev_up.sh              # containers + migrations + API, in order
bash scripts/dev_up.sh --no-api     # containers + migrations only
bash scripts/dev_down.sh            # stop, keep volumes
bash scripts/dev_down.sh --volumes  # stop, destroy all data
```

### Verify

```bash
bash scripts/demo_m1.sh ; echo $?   # 0, with every check printing [PASS]
.venv/Scripts/python -m pytest tests/ -v
```

---

## Ports

| Service | Container port | Host port | URL |
| --- | --- | --- | --- |
| postgres | 5432 | **55432** | `postgresql://…@localhost:55432/memory_system` |
| redis | 6379 | **56379** | `redis://localhost:56379/0` |
| minio | 9000 | 9000 | <http://localhost:9000> |
| minio console | 9001 | 9001 | <http://localhost:9001> |
| prometheus | 9090 | 9090 | <http://localhost:9090> |
| grafana | 3000 | 3000 | <http://localhost:3000> |
| api (host process) | — | 8000 | <http://localhost:8000/health> |

> **Why 55432 / 56379 and not the plan's 5432 / 6379.** This machine runs a
> native PostgreSQL 18 service and a native Memurai (Redis) service on the
> standard ports, and neither can be stopped without an elevated shell. Mapping
> the containers onto those ports would have pointed every probe at the native
> service instead of the container — exactly the failure the plan's Definition
> of Done warns about ("verify there is no native-service or non-container
> check"). Both mappings are env-driven: set `POSTGRES_HOST_PORT=5432` and
> `REDIS_HOST_PORT=6379` in `infra/.env` (and the matching ports in
> `DATABASE_URL` / `REDIS_URL`) once the native services are stopped.

---

## Layout

```
infra/          docker-compose.yml, prometheus.yml, .env.example
store/          migrations/*.sql, db.py (pool + RLS session), migrate.py
llm/            config.py — the single model seam
api/            main.py — /health, /metrics
scripts/        demo_m1.sh, dev_up.sh, dev_down.sh
tests/          integration/, unit/
```

---

## Two things worth knowing before you touch the code

### 1. The app never connects as the database owner

PostgreSQL superusers bypass row-level security unconditionally, and a table's
owner bypasses it unless `FORCE ROW LEVEL SECURITY` is set. So an application
that connects as `POSTGRES_USER` has RLS policies that are decorative —
`test_rls_blocks_cross_subject_read` would pass rows straight through and the
milestone would look green while enforcing nothing.

Two roles, therefore:

- **`memory`** (owner, superuser) — `DATABASE_URL`. Used only by
  `python -m store.migrate`.
- **`memory_app`** (`NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`, owns
  nothing) — created by `0004_rls.sql`, granted only DML on the three tables.
  Everything that reads or writes application data goes through it, via
  `store.db.session(subject_id, actor_id)`.

`FORCE ROW LEVEL SECURITY` is set on all three tables as well, so even the owner
is subject to the policies.

The policy predicate reads two transaction-local GUCs, `app.subject_id` and
`app.actor_id`. When they are unset, `current_setting(…, true)` returns NULL, the
comparison is NULL, and no rows match — RLS fails closed, never open. There is a
test for exactly that.

**Reading the policies:** the plan's Definition of Done suggests

```sql
select tablename, qual from pg_policies where tablename='memories';
```

which shows `qual = NULL` for the INSERT policy. That is a PostgreSQL rule, not
a gap: an INSERT policy may only carry `WITH CHECK`, never `USING`. Use

```sql
select tablename, cmd, coalesce(qual, with_check) from pg_policies where tablename='memories';
```

to see all four predicates. `scripts/demo_m1.sh` prints both forms.

### 2. `max_tokens` below 512 makes gpt-oss look broken

gpt-oss models spend their token budget on internal reasoning *before* emitting
any content. Call one with a small `max_tokens` and `content` comes back an
empty string with `finish_reason='stop'` — it reads as a broken model, but it is
a truncated reasoning phase. `llm/config.py` floors every completion at
`MIN_MAX_TOKENS = 512`. Do not "fix" an empty completion by switching models.

---

## The schema seam

`memories` carries **two** identity columns rather than one `user_id`:

- `subject_id` — whose memory this is, the person the fact is about
- `actor_id` — who wrote or read it

In this single-user assistant they are always equal. The split is a
forward-compatible seam: a future "agent writes about a user" or shared-assistant
model needs new values in a column that already exists, not a table rewrite and
a data migration. Every RLS policy is scoped on both, so the seam is enforced
from day one rather than retrofitted.

## Embedding width is not hardcoded

`EMBEDDING_DIM` is substituted into `embedding vector(N)` by `store/migrate.py`
at migration time. The `.sql` file contains `vector(${EMBEDDING_DIM})`, never a
literal. Moving to a different-width embedder is an env change plus a re-embed,
not a hand-edited migration.

The three Voyage models in play (`voyage-3.5`, `voyage-3.5-lite`,
`voyage-3-large`) are all 1024-dim, so swapping between them needs no migration
at all.
