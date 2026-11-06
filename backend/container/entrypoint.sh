#!/bin/sh
# Entrypoint of the mcpanel/panel image. One image, several process roles:
#
#   web     apply database migrations, then serve HTTP + WebSockets (uvicorn, port 8000)
#   worker  Celery worker (jobs, reconcile loop tasks)
#   beat    Celery beat (periodic task scheduler; run exactly ONE beat per deployment)
#   manage  pass-through to `python manage.py ...` (e.g. `manage createsuperuser`)
#
# Migrations run only in `web` so that worker/beat replicas never race on schema
# changes; Compose starts worker/beat after the web container reports healthy.
# Extra arguments after the mode are appended to the command.
set -eu

usage() {
    echo "usage: mcpanel-entrypoint {web|worker|beat|manage} [args...]" >&2
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

[ "$#" -gt 0 ] || usage
mode="$1"
shift

case "$mode" in
    web)
        wait_for_database
        python manage.py migrate --noinput
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
        set -- celery -A mcpanel worker -l info "$@"
        if [ -n "${MCPANEL_WORKER_CONCURRENCY:-}" ]; then
            if ! is_uint "$MCPANEL_WORKER_CONCURRENCY" || [ "$MCPANEL_WORKER_CONCURRENCY" -lt 1 ]; then
                echo "MCPANEL_WORKER_CONCURRENCY must be a positive integer" >&2
                exit 64
            fi
            set -- "$@" --concurrency "$MCPANEL_WORKER_CONCURRENCY"
        fi
        exec "$@"
        ;;
    beat)
        exec celery -A mcpanel beat -l info --schedule /tmp/celerybeat-schedule "$@"
        ;;
    manage)
        exec python manage.py "$@"
        ;;
    *)
        usage
        ;;
esac
