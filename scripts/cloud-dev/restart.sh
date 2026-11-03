#!/usr/bin/env bash
# Restart one dev service: restart.sh agent|api|worker  (sandbox only)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
case "${1:-}" in
  agent)  PATTERN='[u]vicorn mcagent.main:app --host 127.0.0.1 --port 8700' ;;
  api)    PATTERN='[u]vicorn mcpanel.asgi' ;;
  worker) PATTERN='[c]elery -A mcpanel' ;;
  *) echo "usage: $0 agent|api|worker"; exit 2 ;;
esac
for pid in $(pgrep -f "$PATTERN" || true); do kill "$pid" 2>/dev/null || true; done
sleep 2
case "$1" in
  agent)  ( cd "$ROOT/agent" && set -a && . "$ROOT/scripts/cloud-dev/agent.env" && set +a && \
            nohup uv run uvicorn mcagent.main:app --host 127.0.0.1 --port 8700 > /tmp/agent.log 2>&1 & ) ;;
  api)    ( cd "$ROOT/backend" && set -a && . "$ROOT/scripts/cloud-dev/backend.env" && set +a && \
            nohup uv run uvicorn mcpanel.asgi:application --host 127.0.0.1 --port 8000 --reload --reload-dir apps --reload-dir mcpanel > /tmp/backend.log 2>&1 & ) ;;
  worker) ( cd "$ROOT/backend" && set -a && . "$ROOT/scripts/cloud-dev/backend.env" && set +a && \
            nohup uv run celery -A mcpanel worker -B -l info --concurrency 4 > /tmp/worker.log 2>&1 & ) ;;
esac
echo "restarted $1"
