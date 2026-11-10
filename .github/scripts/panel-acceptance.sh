#!/bin/bash
# Acceptance tests of the single panel container, against a running Compose stack with real
# containers (CI job "docker"; also runs by hand). No Minecraft server is needed.
#
#   cd deploy && ../.github/scripts/panel-acceptance.sh
#
# The stack must be up (docker compose up -d --wait) with MCPANEL_WORKER_CONCURRENCY=4 and
# Redis inside the panel container (the default). DC=<command> replaces "docker compose"
# (a wrapper script that adds -p / -f / --env-file).
set -uo pipefail

dc() { if [ -n "${DC:-}" ]; then "$DC" "$@"; else docker compose "$@"; fi; }
PANEL=$(dc ps -q panel) || exit 1
[ -n "$PANEL" ] || { echo "the panel container is not running"; exit 1; }
NETWORK=$(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{end}}' "$PANEL")
SUP=/usr/local/bin/mcpanel-supervise.py
FAILED=0

pexec() { docker exec "$PANEL" "$@"; }
pass() { printf 'PASS  %s\n' "$*"; }
fail() {
    printf 'FAIL  %s\n' "$*"
    FAILED=1
}
check() { # check DESCRIPTION COMMAND...
    local what="$1"
    shift
    if "$@" >/dev/null 2>&1; then pass "$what"; else fail "$what"; fi
}
py() { docker exec -i "$PANEL" python -; }
verdict() { # verdict STATUS PASS-TEXT [FAIL-TEXT]
    if [ "$1" = 0 ]; then pass "$2"; else fail "${3:-$2}"; fi
}
child() { # child NAME FIELD
    pexec python -c "import json,sys;print(json.load(open('/run/mcpanel/supervisor.json'))['children'][sys.argv[1]][sys.argv[2]])" "$1" "$2"
}
all_pids() { pexec python -c "import json;print(' '.join(f'{k}={v[\"pid\"]}' for k,v in json.load(open('/run/mcpanel/supervisor.json'))['children'].items()))"; }
# shellcheck disable=SC2329 # used through check / wait_for
running() { pexec python "$SUP" status >/dev/null 2>&1; }
healthcheck() { pexec python /usr/local/bin/mcpanel-healthcheck.py all; }
wait_for() { # wait_for SECONDS COMMAND...
    local end=$((SECONDS + $1))
    shift
    while [ "$SECONDS" -lt "$end" ]; do
        "$@" >/dev/null 2>&1 && return 0
        sleep 1
    done
    return 1
}
# shellcheck disable=SC2329 # used through check / wait_for
healthy() { [ "$(docker inspect -f '{{.State.Health.Status}}' "$PANEL")" = healthy ]; }
sql() { docker exec "$(dc ps -q postgres)" sh -c 'psql -X -q -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"' sh "$1"; }

echo "== 1. all children up and healthy"
wait_for 180 healthy
check "the panel container is healthy" healthy
check "the supervisor reports every process running" running
if [ "$(all_pids | tr ' ' '\n' | cut -d= -f1 | tr '\n' ' ')" = "redis web worker periodic beat " ]; then pass "children: redis web worker periodic beat"; else fail "children are: $(all_pids)"; fi
pexec python "$SUP" status
check "/api/health names the processes" sh -c "docker exec $PANEL python /usr/local/bin/mcpanel-healthcheck.py all && docker exec $PANEL python -c \"import urllib.request,json;d=json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/api/health',headers={'Host':'localhost'})));assert d['components']['beat']=='running' and d['tick_age_seconds'] < 60, d\""

echo "== 2. tini, UID 10001, no capabilities"
py <<'PY'
import os
pids = [p for p in os.listdir("/proc") if p.isdigit()]
for pid in pids:
    try:
        status = dict(line.split(":\t", 1) for line in open(f"/proc/{pid}/status").read().splitlines() if ":\t" in line)
    except OSError:
        continue
    assert status["Uid"].split() == ["10001"] * 4, (pid, status["Uid"])
    assert int(status["CapEff"], 16) == 0 and int(status["CapPrm"], 16) == 0 and int(status["CapBnd"], 16) == 0, (pid, status["CapEff"])
    assert status["NoNewPrivs"] == "1", pid
assert open("/proc/1/cmdline").read().split("\0")[0].endswith("docker-init"), open("/proc/1/cmdline").read()
PY
verdict $? "every process runs as UID 10001 with no capability under docker-init" "privileges"

echo "== 3. every log line names its process"
unnamed=$(docker logs "$PANEL" 2>&1 | grep -vcE '^\[(migrate|guard|supervisor|redis|web|worker|periodic|beat)\] ')
if [ "$unnamed" = 0 ]; then
    pass "all $(docker logs "$PANEL" 2>&1 | wc -l) log lines start with a process name"
else
    fail "$unnamed log lines without a name"
    docker logs "$PANEL" 2>&1 | grep -vE '^\[(migrate|guard|supervisor|redis|web|worker|periodic|beat)\] ' | head -5
fi

echo "== 4. Redis: a Unix socket only, nothing on disk, none of the secrets"
py <<'PY'
import os, socket, subprocess
listeners = set()
for family in ("tcp", "tcp6"):
    for line in open(f"/proc/net/{family}").read().splitlines()[1:]:
        fields = line.split()
        if fields[3] == "0A":
            address, port = fields[1].rsplit(":", 1)
            listeners.add((address, int(port, 16)))
# 127.0.0.11 is Docker's own DNS resolver inside every container
others = {(a, p) for a, p in listeners if p != 8000 and a != "0B00007F"}
assert not others, others
for target in ("127.0.0.1", socket.gethostbyname(socket.gethostname())):
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect((target, 6379))
        raise AssertionError(f"{target}:6379 is open")
    except OSError:
        pass
    finally:
        s.close()
cli = ["redis-cli", "-s", "/run/mcpanel/redis.sock"]
assert subprocess.run([*cli, "ping"], capture_output=True, text=True).stdout.strip() == "PONG"
config = subprocess.run([*cli, "config", "get", "*"], capture_output=True, text=True).stdout.split("\n")
config = dict(zip(config[::2], config[1::2]))
assert config["port"] == "0" and config["save"] == "" and config["appendonly"] == "no", (config["port"], config["save"], config["appendonly"])
assert config["unixsocketperm"] == "700", config["unixsocketperm"]
info = os.stat("/run/mcpanel")
assert info.st_uid == 10001 and info.st_mode & 0o777 == 0o700, oct(info.st_mode)
assert os.stat("/run/mcpanel/supervisor.json").st_mode & 0o777 == 0o600
clients = subprocess.run([*cli, "client", "list"], capture_output=True, text=True).stdout
assert clients.count("addr=/run/mcpanel/redis.sock") >= 5, clients  # web, both workers, beat ... all through the socket
PY
verdict $? "no TCP listener except the web server; Redis answers on its socket only" "redis exposure"
check "another container cannot reach a Redis port on the panel" sh -c "! docker run --rm --network $NETWORK --entrypoint python $(docker inspect -f '{{.Config.Image}}' "$PANEL") -c \"import socket;socket.create_connection(('panel',6379),3)\""

echo "== 5. the periodic worker has 2 slots whatever MCPANEL_WORKER_CONCURRENCY is ($(pexec printenv MCPANEL_WORKER_CONCURRENCY))"
py <<'PY'
import json, socket, subprocess
host = socket.gethostname()
out = subprocess.run(["celery", "-A", "mcpanel", "inspect", "stats", "-d", f"celery@{host},periodic@{host}", "-t", "10", "--json"], capture_output=True, text=True).stdout
stats = json.loads(out.strip().splitlines()[-1])
assert stats[f"celery@{host}"]["pool"]["max-concurrency"] == 4, stats[f"celery@{host}"]["pool"]
assert stats[f"periodic@{host}"]["pool"]["max-concurrency"] == 2, stats[f"periodic@{host}"]["pool"]
PY
verdict $? "job worker 4 slots, periodic worker 2" "worker concurrency"

echo "== 6. the scheduler keeps its schedule in memory"
check "beat runs celery.beat.Scheduler" sh -c "docker logs $PANEL 2>&1 | grep -qF '[beat]     . scheduler -> celery.beat.Scheduler'"
check "no schedule file in /tmp" sh -c "! docker exec $PANEL sh -c 'ls /tmp/celerybeat-schedule* 2>/dev/null | grep -q .'"

echo "== 7. kill -9 of each child: it comes back alone (after Redis: all of them)"
STARTED=$(docker inspect -f '{{.RestartCount}}/{{.State.StartedAt}}' "$PANEL")
for name in web beat periodic worker redis; do
    before=$(all_pids)
    pid=$(child "$name" pid)
    pexec sh -c "kill -9 $pid"
    if ! wait_for 60 sh -c "[ \"\$(docker exec $PANEL python -c \"import json;print(json.load(open('/run/mcpanel/supervisor.json'))['children']['$name']['pid'])\")\" != $pid ] && docker exec $PANEL python $SUP status"; then
        fail "$name did not come back: $(all_pids)"
        continue
    fi
    after=$(all_pids)
    changed=$(comm -3 <(tr ' ' '\n' <<<"$before" | sort) <(tr ' ' '\n' <<<"$after" | sort) | tr -d '\t' | cut -d= -f1 | sort -u | tr '\n' ' ')
    if [ "$name" = redis ]; then expected="beat periodic redis web worker "; else expected="$name "; fi
    if [ "$changed" = "$expected" ]; then pass "kill -9 $name: restarted: $changed"; else fail "kill -9 $name: restarted '$changed', expected '$expected'"; fi
    sleep 3
done

echo "== 7b. Redis never loads a dump file: what could be one is removed before every start"
# SAVE on request still writes one (saving by itself is off); the web process could also write the file.
pexec sh -c 'redis-cli -s /run/mcpanel/redis.sock set zz_acceptance_planted 1 >/dev/null && redis-cli -s /run/mcpanel/redis.sock save >/dev/null && test -s /run/mcpanel/dump.rdb'
verdict $? "planted a dump file in /run/mcpanel" "could not plant a dump file"
pid=$(child redis pid)
pexec sh -c "kill -9 $pid"
wait_for 60 sh -c "[ \"\$(docker exec $PANEL python -c \"import json;print(json.load(open('/run/mcpanel/supervisor.json'))['children']['redis']['pid'])\")\" != $pid ] && docker exec $PANEL python $SUP status"
verdict $? "Redis and the others are back" "Redis did not come back: $(all_pids)"
check "the dump file is gone" sh -c "! docker exec $PANEL test -e /run/mcpanel/dump.rdb"
check "Redis did not load it" sh -c "[ \"\$(docker exec $PANEL redis-cli -s /run/mcpanel/redis.sock exists zz_acceptance_planted)\" = 0 ]"
check "the supervisor said that it removed it" sh -c "docker logs --since 120s $PANEL 2>&1 | grep -F '[supervisor]' | grep -qF 'removed dump.rdb'"
sleep 3
check "the container was never restarted by Docker: the supervisor handled all of it" sh -c "[ \"\$(docker inspect -f '{{.RestartCount}}/{{.State.StartedAt}}' $PANEL)\" = '$STARTED' ]"

echo "== 8. a queued job survives a restart of the panel container: it is sent again"
# A QUEUED row of a kind no worker knows: the worker that gets its message fails it as unknown_job_kind.
job=$(sql "INSERT INTO jobs_job (id, kind, title, status, exclusive, progress, message, error, error_code, params, result, log, agent_operation_id, created_at) VALUES (gen_random_uuid(), 'zz_acceptance_probe', 'acceptance probe', 'queued', false, 0, '', '', '', '{}', '{}', '[]', '', now()) RETURNING id" | head -n 1)
sleep 5
if [ "$(sql "SELECT status FROM jobs_job WHERE id = '$job'")" = queued ]; then pass "job $job waits (no message was ever sent for it)"; else fail "the probe job did not stay queued"; fi
dc restart panel >/dev/null 2>&1
wait_for 90 sh -c "[ \"\$(docker exec $(dc ps -q postgres) sh -c 'psql -X -q -At -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -c \"SELECT error_code FROM jobs_job WHERE id = '\\''$job'\\''\"')\" = unknown_job_kind ]"
state=$(sql "SELECT status || ' ' || error_code FROM jobs_job WHERE id = '$job'")
if [ "$state" = "failed unknown_job_kind" ]; then pass "after the restart the job worker got it ($state)"; else fail "the queued job was not picked up after the restart: $state"; fi
check "the worker logged the re-send" sh -c "docker logs $PANEL 2>&1 | grep -F '[worker]' | grep -qF 'queued job(s) again'"
sql "DELETE FROM jobs_job WHERE id = '$job'" >/dev/null
wait_for 120 healthy
check "healthy again after the restart" healthy

echo "== 9. health check: unhealthy with a reason, and back"
since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
pexec sh -c 'dd if=/dev/zero of=/run/mcpanel/fill bs=1M count=32 2>/dev/null; true'   # the status file cannot be written any more
sleep 40
out=$(healthcheck 2>&1)
code=$?
[ "$code" = 1 ] && grep -q "supervisor: its status is" <<<"$out"
verdict $? "status cannot be written: $out" "expected unhealthy, got $code: $out"
check "the supervisor went on supervising (it logged the problem once)" sh -c "[ \"\$(docker logs --since $since $PANEL 2>&1 | grep -c 'cannot write the status file')\" = 1 ]"
pexec rm -f /run/mcpanel/fill
wait_for 30 healthcheck
verdict $? "healthy again once the status file can be written" "still unhealthy: $(healthcheck 2>&1)"

echo "== 10. docker stop: in order, well inside the grace period"
began=$SECONDS
docker stop "$PANEL" >/dev/null
took=$((SECONDS - began))
code=$(docker inspect -f '{{.State.ExitCode}}' "$PANEL")
if [ "$took" -lt 60 ] && [ "$code" = 0 ]; then pass "stopped in $took s with exit code 0"; else fail "stop took $took s, exit code $code"; fi
order=$(docker logs --tail 40 "$PANEL" 2>&1 | grep -F '[supervisor]' | grep -oE '(redis|web|worker|periodic|beat) stopped' | cut -d' ' -f1 | tr '\n' ' ')
case "$order" in
    *"periodic redis ") case "$order" in *worker*periodic*) pass "stop order: $order" ;; *) fail "stop order: $order" ;; esac ;;
    *) fail "stop order: $order" ;;
esac

echo "== 11. start-up guard: refuses next to a worker container of the previous layout"
docker run -d --rm --name "${PANEL:0:12}-old-worker" --network "$NETWORK" --network-alias worker --entrypoint sleep "$(docker inspect -f '{{.Config.Image}}' "$PANEL")" 300 >/dev/null
docker start "$PANEL" >/dev/null
wait_for 60 sh -c "docker logs --tail 30 $PANEL 2>&1 | grep -q 'containers of the previous layout are still running: worker'"
verdict $? "refused: $(docker logs --tail 30 "$PANEL" 2>&1 | grep -m1 'previous layout')" "the guard did not refuse"
check "no process was started next to the old worker" sh -c "! docker logs --since 50s $PANEL 2>&1 | grep -q 'started web'"
docker rm -f "${PANEL:0:12}-old-worker" >/dev/null
docker restart "$PANEL" >/dev/null 2>&1 || docker start "$PANEL" >/dev/null
wait_for 180 healthy
verdict $? "healthy again once the old container is gone" "not healthy after the guard test: $(docker inspect -f '{{range .State.Health.Log}}{{.Output}}{{end}}' "$PANEL" | tail -n 1)"

echo
if [ "$FAILED" = 0 ]; then echo "ALL ACCEPTANCE TESTS PASSED"; else echo "SOME ACCEPTANCE TESTS FAILED"; docker logs --tail 60 "$PANEL" 2>&1; fi
exit "$FAILED"
