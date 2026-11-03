#!/usr/bin/env bash
# Starts agent, API (uvicorn) and worker (celery -B) for local development in the
# cloud sandbox. Logs go to /tmp/{agent,backend,worker}.log.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
pkill -f "uvicorn mcagent.main" || true
pkill -f "uvicorn mcpanel.asgi" || true
pkill -f "celery -A mcpanel" || true
sleep 1
( cd "$ROOT/agent" && set -a && . "$ROOT/scripts/cloud-dev/agent.env" && set +a && \
  nohup uv run uvicorn mcagent.main:app --host 127.0.0.1 --port 8700 > /tmp/agent.log 2>&1 & )
( cd "$ROOT/backend" && set -a && . "$ROOT/scripts/cloud-dev/backend.env" && set +a && \
  nohup uv run uvicorn mcpanel.asgi:application --host 127.0.0.1 --port 8000 --reload --reload-dir apps --reload-dir mcpanel > /tmp/backend.log 2>&1 & )
( cd "$ROOT/backend" && set -a && . "$ROOT/scripts/cloud-dev/backend.env" && set +a && \
  nohup uv run celery -A mcpanel worker -B -l info --concurrency 4 > /tmp/worker.log 2>&1 & )
echo "started"
