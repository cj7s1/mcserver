#!/usr/bin/env bash
# Bring the cloud-sandbox dev environment back after a container restart.
# Idempotent: safe to run when some pieces are already up. Sandbox only.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

pgrep -x dockerd >/dev/null || (dockerd > /tmp/dockerd.log 2>&1 &)
service postgresql start >/dev/null 2>&1 || true
service redis-server start >/dev/null 2>&1 || true
until docker info >/dev/null 2>&1; do sleep 1; done

# btrfs loopback used by the second node (snapshot tests)
if [ -f /srv/btrfs.img ] && ! mountpoint -q /srv/btrfs-test; then
  mkdir -p /srv/btrfs-test && mount -o loop /srv/btrfs.img /srv/btrfs-test
fi

# containers reach the sandbox HTTPS proxy through this forwarder (see egress_forwarder.py)
pgrep -f "[e]gress_forwarder.py" >/dev/null || \
  (nohup python3 "$ROOT/scripts/cloud-dev/egress_forwarder.py" 0.0.0.0 3128 127.0.0.1 37231 > /tmp/forwarder.log 2>&1 &)

"$ROOT/scripts/cloud-dev/start-services.sh"

# second agent ("btrfs-node") on :8701
if [ -d /srv/btrfs-test ] && ! pgrep -f "[m]cagent.main:app --host 127.0.0.1 --port 8701" >/dev/null; then
  (cd "$ROOT/agent" && set -a && . "$ROOT/scripts/cloud-dev/agent.env" && \
   AGENT_DATA_ROOT=/srv/btrfs-test/data AGENT_TOKEN=btrfs-node-token && set +a && \
   nohup uv run uvicorn mcagent.main:app --host 127.0.0.1 --port 8701 > /tmp/agent2.log 2>&1 &)
fi

# frontend dev server
pgrep -f "[v]ite --port 5173" >/dev/null || \
  (cd "$ROOT/frontend" && nohup npx vite --port 5173 --host 127.0.0.1 > /tmp/vite.log 2>&1 &)

for url in http://127.0.0.1:8700/v1/health http://127.0.0.1:8000/api/health; do
  for _ in $(seq 1 60); do curl -sf "$url" >/dev/null && break; sleep 1; done
done
echo "bootstrap done: agent :8700, agent2 :8701, api :8000, vite :5173"
