#!/bin/sh
# Entrypoint of the mcpanel/panel image. One image, several process roles:
#
#   all     everything in one container (what deploy/docker-compose.yml runs): apply database
#           migrations, then start the supervisor (mcpanel-supervise.py), which runs Redis
#           (unless MCPANEL_REDIS_URL points to a server elsewhere), the web server, the job
#           worker, the periodic worker and the scheduler as its child processes
#   web     apply database migrations, then serve HTTP + WebSockets (uvicorn, port 8000)
#   worker  Celery worker (jobs, reconcile loop tasks)
#   beat    Celery beat (periodic task scheduler; run exactly ONE beat per deployment)
#   manage  pass-through to `python manage.py ...` (e.g. `manage createsuperuser`)
#   Used by `all` for its steps:
#   migrate wait for the database, apply the migrations, exit
#   serve   the web server alone: the same command as `web`, without the migration step
#
# web / worker / beat are the one-role-per-container layout (the stack before the single
# panel container, and what a rollback returns to). There, migrations run only in `web` so
# that worker/beat replicas never race on schema changes; Compose starts worker/beat after
# the web container reports healthy.
# Extra arguments after the mode are appended to the command.
set -eu

usage() {
    echo "usage: mcpanel-entrypoint {all|web|worker|beat|manage} [args...]" >&2
    exit 64
}

# Same truthiness rules as env_bool() in mcpanel/settings.py.
is_true() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1 | true | yes | on) return 0 ;;
        *) return 1 ;;
    esac
}

is_uint() {
    case "${1:-}" in
        '' | *[!0-9]*) return 1 ;;
        *) return 0 ;;
    esac
}

# has_option SHORT LONG ARGS...: true when ARGS contain the option in any spelling
# (-c 2, -c2, --concurrency 2, --concurrency=2).
has_option() {
    opt_short="$1"
    opt_long="$2"
    shift 2
    for opt_arg in "$@"; do
        case "$opt_arg" in
            "$opt_short" | "$opt_short"[!-]* | "$opt_long" | "$opt_long"=*) return 0 ;;
        esac
    done
    return 1
}

# Wait for PostgreSQL so a host reboot (where Docker restarts containers without
# Compose's depends_on ordering) does not crash-loop the web container.
wait_for_database() {
    timeout="${MCPANEL_DB_WAIT_SECONDS:-60}"
    if ! is_uint "$timeout"; then
        echo "MCPANEL_DB_WAIT_SECONDS must be a non-negative integer" >&2
        exit 64
    fi
    MCPANEL_DB_WAIT_SECONDS="$timeout" python - <<'PY'
import os
import sys
import time

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mcpanel.settings")
django.setup()

from django.db import connection
from django.db.utils import OperationalError

deadline = time.monotonic() + int(os.environ["MCPANEL_DB_WAIT_SECONDS"])
while True:
    try:
        connection.ensure_connection()
        break
    except OperationalError as exc:
        if time.monotonic() >= deadline:
            print(f"Database is not reachable: {exc}", file=sys.stderr)
            sys.exit(1)
        print("Waiting for the database...", file=sys.stderr)
        time.sleep(2)
connection.close()
PY
}

# The spool folder of request bodies and uploads (settings.FILE_UPLOAD_TEMP_DIR), made by the
# runtime user itself: a folder the image build left there would belong to root, and the web
# process could not spool any body above 5 MB (the web process checks it again at start, and
# /api/health fails while it is not writable).
ensure_upload_dir() {
    upload_dir="${MCPANEL_UPLOAD_TMP:-/tmp/mcpanel-uploads}"
    if ! mkdir -p -m 0700 "$upload_dir" 2>/dev/null || [ ! -w "$upload_dir" ]; then
        echo "mcpanel: the upload folder $upload_dir (MCPANEL_UPLOAD_TMP) is not writable by uid $(id -u):" \
            "every upload or request body above 5 MB will fail. Point MCPANEL_UPLOAD_TMP to a folder this user owns." >&2
    fi
}

[ "$#" -gt 0 ] || usage
mode="$1"
shift

# Every mode: Django's system checks (run by `manage check`, `migrate`, and the upgrade script's
# `manage migrate --plan`) refuse to start when FILE_UPLOAD_TEMP_DIR does not exist (files.E001),
# and the image no longer contains the folder (it used to be created as root at build time).
ensure_upload_dir

case "$mode" in
    all)
        [ "$#" -eq 0 ] || usage
        supervise="${MCPANEL_SUPERVISE:-/usr/local/bin/mcpanel-supervise.py}"
        if [ -z "${MCPANEL_REDIS_URL:-}" ]; then
            # No Redis configured: run it in this container, on a Unix socket only.
            export MCPANEL_REDIS_URL="unix:///run/mcpanel/redis.sock?db=0"
            export MCPANEL_CELERY_BROKER_URL="redis+socket:///run/mcpanel/redis.sock"
        fi
        # Old worker / worker-periodic / beat containers still running next to this one?
        python "$supervise" guard
        # Like everything the supervisor starts, the migration step has its name in front of
        # every line it prints.
        python "$supervise" step migrate mcpanel-entrypoint migrate
        exec python "$supervise" run
        ;;
    migrate)
        [ "$#" -eq 0 ] || usage
        wait_for_database
        exec python manage.py migrate --noinput
        ;;
    web | serve)
        if [ "$mode" = web ]; then
            wait_for_database
            python manage.py migrate --noinput
        fi
        set -- uvicorn mcpanel.asgi:application \
            --host 0.0.0.0 --port 8000 \
            --lifespan off --no-server-header "$@"
        if is_true "${MCPANEL_BEHIND_TLS_PROXY:-}"; then
            # Trust X-Forwarded-For/-Proto only behind a reverse proxy, and only from
            # peers in MCPANEL_FORWARDED_ALLOW_IPS: the proxy's address as this container
            # sees it. The default is loopback only. Private ranges are NOT trusted by
            # default: Minecraft containers (third-party mod code) and other LAN hosts
            # live there and could otherwise fake their address (login throttle, audit
            # log). uvicorn takes the client address as the right-most X-Forwarded-For
            # entry that is not a trusted peer; REST_FRAMEWORK["NUM_PROXIES"] = 0 keeps
            # Django from parsing the header again. Do not use "*": uvicorn would take the
            # LEFT-most entry, which the client controls whenever the proxy appends to
            # X-Forwarded-For (nginx, Nginx Proxy Manager, SWAG).
            if [ -z "${MCPANEL_FORWARDED_ALLOW_IPS:-}" ]; then
                echo "mcpanel: MCPANEL_BEHIND_TLS_PROXY=true but MCPANEL_FORWARDED_ALLOW_IPS is empty:" \
                    "only 127.0.0.1/::1 are trusted, so every request appears to come from the proxy" \
                    "(one shared login rate limit). Set it to the proxy's address (see DEPLOYMENT.md)." >&2
            fi
            set -- "$@" --proxy-headers --forwarded-allow-ips \
                "${MCPANEL_FORWARDED_ALLOW_IPS:-127.0.0.1,::1}"
        else
            set -- "$@" --no-proxy-headers
        fi
        exec "$@"
        ;;
    worker)
        # MCPANEL_WORKER_CONCURRENCY applies only when the arguments set no concurrency
        # themselves: the periodic worker (`worker -Q periodic -c 2 ...`) always has 2 slots.
        # Celery takes the last value of a repeated option, so appending it would win.
        concurrency=""
        if ! has_option -c --concurrency "$@" && [ -n "${MCPANEL_WORKER_CONCURRENCY:-}" ]; then
            if ! is_uint "$MCPANEL_WORKER_CONCURRENCY" || [ "$MCPANEL_WORKER_CONCURRENCY" -lt 1 ]; then
                echo "MCPANEL_WORKER_CONCURRENCY must be a positive integer" >&2
                exit 64
            fi
            concurrency="$MCPANEL_WORKER_CONCURRENCY"
        fi
        if has_option -l --loglevel "$@"; then
            set -- celery -A mcpanel worker "$@"
        else
            set -- celery -A mcpanel worker -l info "$@"
        fi
        if [ -n "$concurrency" ]; then
            set -- "$@" --concurrency "$concurrency"
        fi
        exec "$@"
        ;;
    beat)
        # The default scheduler keeps its state in a file (a pickle that is loaded at start).
        # `all` passes `-S celery.beat:Scheduler` instead: the schedule stays in memory, so
        # there is no such file next to the web process.
        if ! has_option -S --scheduler "$@"; then
            set -- --schedule /tmp/celerybeat-schedule "$@"
        fi
        if ! has_option -l --loglevel "$@"; then
            set -- -l info "$@"
        fi
        exec celery -A mcpanel beat "$@"
        ;;
    manage)
        exec python manage.py "$@"
        ;;
    *)
        usage
        ;;
esac
