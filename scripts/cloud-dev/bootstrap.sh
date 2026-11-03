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

# containers reach the sandbox HTTPS proxy through this forwarder (see egress_forwarder.py).
# The proxy's local port changes between container restarts: take it from $HTTPS_PROXY.
proxy_port=$(printf '%s' "${HTTPS_PROXY:-http://127.0.0.1:37231}" | sed -E 's#^.*:([0-9]+)/?$#\1#')
if ! pgrep -f "[e]gress_forwarder.py 0.0.0.0 3128 127.0.0.1 $proxy_port\$" >/dev/null; then
  pkill -f "[e]gress_forwarder.py" || true
  (nohup python3 "$ROOT/scripts/cloud-dev/egress_forwarder.py" 0.0.0.0 3128 127.0.0.1 "$proxy_port" > /tmp/forwarder.log 2>&1 &)
fi

"$ROOT/scripts/cloud-dev/start-services.sh"

# second agent ("btrfs-node") on :8701, served over HTTPS (AGENT_TLS=auto); the panel pins
# its certificate (fingerprint: AGENT_DATA_ROOT=/srv/btrfs-test/data AGENT_TLS=auto
# uv run python -m mcagent fingerprint)
if [ -d /srv/btrfs-test ] && ! curl -skf https://127.0.0.1:8701/v1/health >/dev/null; then
  (cd "$ROOT/agent" && set -a && . "$ROOT/scripts/cloud-dev/agent.env" && \
   AGENT_DATA_ROOT=/srv/btrfs-test/data AGENT_TOKEN=btrfs-node-token AGENT_TLS=auto \
   AGENT_LISTEN_HOST=127.0.0.1 AGENT_LISTEN_PORT=8701 && set +a && \
   nohup uv run python -m mcagent > /tmp/agent2.log 2>&1 &)
fi

# frontend dev server
pgrep -f "[v]ite --port 5173" >/dev/null || \
  (cd "$ROOT/frontend" && nohup npx vite --port 5173 --host 127.0.0.1 > /tmp/vite.log 2>&1 &)

for url in http://127.0.0.1:8700/v1/health http://127.0.0.1:8000/api/health; do
  for _ in $(seq 1 60); do curl -sf "$url" >/dev/null && break; sleep 1; done
done
echo "bootstrap done: agent :8700, agent2 :8701, api :8000, vite :5173"
