#!/usr/bin/env bash
# dev_up.sh — bring the whole M1 stack up: containers, then migrations, then API.
#
#   bash scripts/dev_up.sh          # containers + migrations, API in foreground
#   bash scripts/dev_up.sh --no-api # containers + migrations only

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMPOSE="docker compose -f infra/docker-compose.yml"
START_API=1
[ "${1:-}" = "--no-api" ] && START_API=0

PY="$ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python"

if [ -f infra/.env ]; then
  set -a; . ./infra/.env; set +a
fi

echo "== 1/3 containers =="
$COMPOSE up -d

echo "-- waiting for healthchecks --"
for _ in $(seq 1 60); do
  unhealthy="$($COMPOSE ps --format '{{.Service}} {{.Health}}' \
                 | awk '$2 != "healthy" {print $1}' || true)"
  [ -z "$unhealthy" ] && break
  sleep 3
done
$COMPOSE ps

echo
echo "== 2/3 migrations =="
"$PY" -m store.migrate

echo
if [ "$START_API" -eq 1 ]; then
  echo "== 3/3 api (ctrl-c to stop) =="
  exec "$PY" -m uvicorn api.main:app --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-8000}"
else
  echo "== 3/3 api skipped (--no-api) =="
  echo "start it with: .venv/Scripts/python -m uvicorn api.main:app --port ${API_PORT:-8000}"
fi
