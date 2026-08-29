#!/usr/bin/env bash
# demo_m1.sh — the M1 milestone demo.
#
# Every service check below is a plain TCP or HTTP probe against a *container*
# host port read from infra/.env (or the compose defaults). Nothing here probes
# a native OS service, and nothing here shells into a container to ask it about
# itself. If the container is down the probe fails, full stop.
#
#   bash scripts/demo_m1.sh ; echo $?
#
# Exits 0 only when every check passes.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FAILURES=0
PASSES=0

pass() { printf '  [PASS] %s\n' "$1"; PASSES=$((PASSES + 1)); }
fail() { printf '  [FAIL] %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
hdr()  { printf '\n== %s ==\n' "$1"; }

# ---------------------------------------------------------------------------
# env
# ---------------------------------------------------------------------------
if [ -f infra/.env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./infra/.env
  set +a
fi

PG_PORT="${POSTGRES_HOST_PORT:-55432}"
REDIS_PORT="${REDIS_HOST_PORT:-56379}"
MINIO_PORT="${MINIO_HOST_PORT:-9000}"
MINIO_CONSOLE_PORT="${MINIO_CONSOLE_HOST_PORT:-9001}"
PROM_PORT="${PROMETHEUS_HOST_PORT:-9090}"
GRAFANA_PORT="${GRAFANA_HOST_PORT:-3000}"
API_PORT_="${API_PORT:-8000}"

PY="$ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python"

printf 'memory-system — M1 demo\n'
printf 'repo: %s\n' "$ROOT"

# ---------------------------------------------------------------------------
# 1. container ports — plain TCP / HTTP probes
# ---------------------------------------------------------------------------
hdr "container port probes (TCP/HTTP only)"

tcp_probe() {
  # $1 = label, $2 = host, $3 = port
  if "$PY" - "$2" "$3" <<'PYEOF'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
s = socket.socket()
s.settimeout(5)
try:
    s.connect((host, port))
except OSError:
    sys.exit(1)
finally:
    s.close()
sys.exit(0)
PYEOF
  then
    pass "$1 — TCP 127.0.0.1:$3 open"
  else
    fail "$1 — TCP 127.0.0.1:$3 refused"
  fi
}

http_probe() {
  # $1 = label, $2 = url, $3 = expected-substring-in-status (optional)
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$2" || echo 000)"
  case "$code" in
    2*|3*) pass "$1 — HTTP $code from $2" ;;
    *)     fail "$1 — HTTP $code from $2" ;;
  esac
}

tcp_probe  "postgres  (container)" 127.0.0.1 "$PG_PORT"
tcp_probe  "redis     (container)" 127.0.0.1 "$REDIS_PORT"
http_probe "minio     (container)" "http://127.0.0.1:${MINIO_PORT}/minio/health/live"
tcp_probe  "minio-ui  (container)" 127.0.0.1 "$MINIO_CONSOLE_PORT"
http_probe "prometheus(container)" "http://127.0.0.1:${PROM_PORT}/-/healthy"
http_probe "grafana   (container)" "http://127.0.0.1:${GRAFANA_PORT}/api/health"

# ---------------------------------------------------------------------------
# 2. FastAPI /health
# ---------------------------------------------------------------------------
hdr "FastAPI /health"

HEALTH_BODY="$(curl -sf --max-time 10 "http://127.0.0.1:${API_PORT_}/health" || true)"
HEALTH_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${API_PORT_}/health" || echo 000)"

if [ "$HEALTH_CODE" = "200" ]; then
  pass "GET /health -> 200"
else
  fail "GET /health -> $HEALTH_CODE"
fi

printf '  body: %s\n' "$HEALTH_BODY"

case "$HEALTH_BODY" in
  *'"postgres":true'*) pass "/health reports postgres=true" ;;
  *)                   fail "/health reports postgres!=true" ;;
esac
case "$HEALTH_BODY" in
  *'"redis":true'*) pass "/health reports redis=true" ;;
  *)                fail "/health reports redis!=true" ;;
esac

# ---------------------------------------------------------------------------
# 3. LiteLLM completion + embedding through llm/config.py
# ---------------------------------------------------------------------------
hdr "LiteLLM seam (llm/config.py)"

LLM_OUT="$("$PY" -m llm.config 2>&1)"
printf '%s\n' "$LLM_OUT" | sed 's/^/  /'

case "$LLM_OUT" in
  *'"completion_ok": true'*) pass "LiteLLM completion returned non-empty text" ;;
  *)                         fail "LiteLLM completion failed or returned empty" ;;
esac
case "$LLM_OUT" in
  *'"embedding_ok": true'*) pass "LiteLLM embedding returned a full-width vector" ;;
  *)                        fail "LiteLLM embedding failed or wrong dimension" ;;
esac

# ---------------------------------------------------------------------------
# 4. schema facts (indexes, seam columns, RLS)
# ---------------------------------------------------------------------------
hdr "schema (indexes / seam columns / RLS)"

SCHEMA_OUT="$("$PY" - <<'PYEOF' 2>&1
import psycopg
from store.db import admin_dsn

with psycopg.connect(admin_dsn()) as conn, conn.cursor() as cur:
    cur.execute("select indexname, indexdef from pg_indexes where tablename='memories'")
    idx = cur.fetchall()
    print("HNSW_ON_EMBEDDING=%s" % any(
        "USING hnsw" in d and "embedding" in d for _, d in idx))
    print("GIN_ON_CONTENT_TSV=%s" % any(
        "USING gin" in d and "content_tsv" in d for _, d in idx))

    cur.execute("select column_name from information_schema.columns "
                "where table_name='memories'")
    cols = {r[0] for r in cur.fetchall()}
    print("SEAM_SUBJECT_AND_ACTOR=%s" % ({"subject_id", "actor_id"} <= cols))
    print("NO_SINGLE_USER_ID=%s" % ("user_id" not in cols))

    cur.execute("select relrowsecurity from pg_class where relname='memories'")
    print("RLS_ENABLED=%s" % cur.fetchone()[0])

    cur.execute("select policyname, cmd, coalesce(qual, with_check) "
                "from pg_policies where tablename='memories'")
    pols = cur.fetchall()
    print("POLICY_COUNT=%d" % len(pols))
    ok = bool(pols) and all(
        "subject_id" in q and "actor_id" in q for _, _, q in pols)
    print("ALL_POLICIES_SCOPE_BOTH=%s" % ok)
    for name, cmd, q in sorted(pols):
        print("  policy %-24s %-6s %s" % (name, cmd, " ".join(q.split())))

    cur.execute("select relname, relrowsecurity from pg_class "
                "where relname in ('audit_log','feedback')")
    for name, rls in sorted(cur.fetchall()):
        print("TABLE_%s_RLS=%s" % (name.upper(), rls))
PYEOF
)"
printf '%s\n' "$SCHEMA_OUT" | sed 's/^/  /'

check_flag() {
  case "$SCHEMA_OUT" in
    *"$1=True"*) pass "$2" ;;
    *)           fail "$2" ;;
  esac
}
check_flag HNSW_ON_EMBEDDING        "HNSW index present on memories.embedding"
check_flag GIN_ON_CONTENT_TSV       "GIN index present on memories.content_tsv"
check_flag SEAM_SUBJECT_AND_ACTOR   "both subject_id and actor_id columns exist"
check_flag NO_SINGLE_USER_ID        "no single user_id column (seam preserved)"
check_flag RLS_ENABLED              "row-level security enabled on memories"
check_flag ALL_POLICIES_SCOPE_BOTH  "every policy qualifier names subject_id AND actor_id"
check_flag TABLE_AUDIT_LOG_RLS      "RLS enabled on audit_log"
check_flag TABLE_FEEDBACK_RLS       "RLS enabled on feedback"

# ---------------------------------------------------------------------------
hdr "summary"
printf '  passed: %d\n  failed: %d\n' "$PASSES" "$FAILURES"

if [ "$FAILURES" -ne 0 ]; then
  printf '\nDEMO FAILED\n'
  exit 1
fi
printf '\nDEMO OK\n'
exit 0
