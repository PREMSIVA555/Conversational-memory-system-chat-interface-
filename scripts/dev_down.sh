#!/usr/bin/env bash
# dev_down.sh — stop the stack.
#
#   bash scripts/dev_down.sh            # stop containers, KEEP volumes
#   bash scripts/dev_down.sh --volumes  # stop and DESTROY all data

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMPOSE="docker compose -f infra/docker-compose.yml"

if [ "${1:-}" = "--volumes" ]; then
  echo "!! destroying named volumes (pgdata, redisdata, miniodata, promdata, grafanadata)"
  $COMPOSE down -v
else
  $COMPOSE down
  echo "volumes kept. re-run scripts/dev_up.sh to come back with data intact."
fi
