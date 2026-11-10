#!/bin/bash
# Rehearsal of deploy/scripts/upgrade.sh with real containers (CI job "upgrade"; also by hand):
#
#   the layout of an older revision, with an owner account and a changed setting
#   -> status and dry-run change nothing
#   -> run refuses while a job runs, and refuses a schema change without --with-migrations
#   -> run: the new layout, the account and the setting are still there, no old container left
#   -> rollback: the old layout and the old images again, schema changes undone, data still there
#   -> run again
#
#   upgrade-rehearsal.sh SITE NEW_REV [URL]
#
# SITE     a git checkout of the OLD revision on a branch, with deploy/.env in place (and, if
#          wanted, deploy/docker-compose.override.yml). Its stack must not be running yet.
# NEW_REV  the revision to upgrade to; SITE's branch is fast-forwarded to it, like `git pull`.
# URL      where the panel answers (default http://127.0.0.1:8080).
set -uo pipefail

SITE=${1:?usage: upgrade-rehearsal.sh SITE NEW_REV [URL]}
NEW_REV=${2:?usage: upgrade-rehearsal.sh SITE NEW_REV [URL]}
URL=${3:-http://127.0.0.1:8080}
DEPLOY="$SITE/deploy"
USERNAME=rehearsal-owner
PASSWORD='Rehearsal-Horse-Battery-42'
JAR=$(mktemp)
OUT=$(mktemp)
trap 'rm -f "$JAR" "$OUT"' EXIT

say() { printf '\n######## %s\n' "$*"; }
ok() { printf 'OK    %s\n' "$*"; }
die() {
    printf 'FAILED  %s\n' "$*" >&2
    (cd "$DEPLOY" && docker compose ps -a && docker compose logs --no-color --tail 40 panel) >&2 2>/dev/null
    exit 1
}
dc() { (cd "$DEPLOY" && docker compose "$@"); }
upgrade() { (cd "$DEPLOY" && ./scripts/upgrade.sh "$@"); }
g() { git -C "$SITE" "$@"; }

services() { dc ps --services --status running | sort | tr '\n' ' '; }
containers() { docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.ID}} {{.Label "com.docker.compose.service"}}' | sort -k2; }
expect_services() {
    [ "$(services)" = "$1" ] || die "running services are '$(services)', expected '$1'"
    ok "running services: $1"
}
sql() { docker exec "$(dc ps -q postgres)" sh -c 'psql -X -q -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"' sh "$1"; }
csrf() { grep mcpanel_csrftoken "$JAR" | tr '\t' ' ' | rev | cut -d' ' -f1 | rev; }
api() { # api METHOD PATH [JSON] -> HTTP status; body in $OUT
    local method="$1" path="$2" body="${3:-}"
    curl -sS -o "$OUT" -w '%{http_code}' -b "$JAR" -c "$JAR" -X "$method" \
        -H "X-CSRFToken: $(csrf)" -H "Origin: $URL" -H "Referer: $URL/" -H 'Content-Type: application/json' \
        ${body:+-d "$body"} "$URL$path"
}
login() {
    rm -f "$JAR"
    curl -fsS -c "$JAR" -o /dev/null "$URL/api/auth/state" || return 1
    [ "$(api POST /api/auth/login "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\"}")" = 200 ]
}
data_is_there() { # the account logs in and the changed setting has its value
    login || die "$1: the owner account cannot log in"
    [ "$(api GET /api/system/settings)" = 200 ] || die "$1: /api/system/settings answered $(cat "$OUT")"
    grep -q '"default_memory_mb": *3072' "$OUT" || die "$1: the changed setting is gone: $(cat "$OUT")"
    [ "$(sql "SELECT count(*) FROM accounts_user")" = 1 ] || die "$1: expected exactly one user"
    ok "$1: the owner account logs in, the changed setting is there, $(sql "SELECT count(*) FROM audit_auditevent") audit entries"
}

PROJECT=$(dc config | sed -n 's/^name: //p' | head -n 1)
[ -n "$PROJECT" ] || die "cannot read the project name"
OLD_REV=$(g rev-parse HEAD)
BRANCH=$(g symbolic-ref --short HEAD) || die "SITE must be on a branch"
[ -z "$(containers)" ] || die "the stack of project $PROJECT is already running"

say "1. the old layout ($(g log -1 --format='%h %s')) comes up"
dc build >/dev/null 2>&1 || die "building the old revision failed"
dc up -d --wait --wait-timeout 300 >/dev/null 2>&1 || die "the old layout did not come up"
OLD_SERVICES=$(services)
ok "running services: $OLD_SERVICES"
OLD_PANEL_IMAGE=$(docker inspect -f '{{.Image}}' "$(dc ps -q panel)")
curl -fsS -c "$JAR" -o /dev/null "$URL/api/auth/state" || die "the panel does not answer at $URL"
[ "$(api POST /api/auth/setup "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\"}")" = 201 ] || die "creating the owner account failed: $(cat "$OUT")"
[ "$(api PATCH /api/system/settings '{"values":{"default_memory_mb":3072}}')" = 200 ] || die "changing a setting failed: $(cat "$OUT")"
data_is_there "old layout"

say "2. the new source arrives (git pull)"
g merge --ff-only "$NEW_REV" >/dev/null || die "cannot fast-forward $BRANCH to $NEW_REV"
ok "source tree: $(g log -1 --format='%h %s')"

say "3. status and dry-run change nothing"
before="$(containers) $(docker images -q --no-trunc | sort | tr '\n' ' ') $(ls "$DEPLOY")"
upgrade status || die "status failed"
upgrade dry-run || die "dry-run failed"
after="$(containers) $(docker images -q --no-trunc | sort | tr '\n' ' ') $(ls "$DEPLOY")"
[ "$before" = "$after" ] || die "status or dry-run changed something"
ok "same containers, same images, no new file in deploy/"
MIGRATES=0
plan=$(upgrade dry-run 2>/dev/null)   # (not piped into grep -q: with pipefail an early exit of grep fails the pipe)
case "$plan" in *"database migrations in this version"*) MIGRATES=1 ;; esac

say "4. run refuses while a job is running"
job=$(sql "INSERT INTO jobs_job (id, kind, title, status, exclusive, progress, message, error, error_code, params, result, log, agent_operation_id, created_at, started_at, heartbeat_at) VALUES (gen_random_uuid(), 'backup', 'rehearsal', 'running', false, 0, '', '', '', '{}', '{}', '[]', '', now(), now(), now()) RETURNING id" | head -n 1)
upgrade run
code=$?
[ "$code" = 2 ] || die "run should refuse with exit code 2 while a job runs, got $code"
[ "$(services)" = "$OLD_SERVICES" ] || die "the refused run changed the layout"
ok "refused (exit 2); the old layout is untouched"
sql "DELETE FROM jobs_job WHERE id = '$job'" >/dev/null

if [ "$MIGRATES" = 1 ]; then
    say "5. run refuses a database schema change without --with-migrations"
    upgrade run
    code=$?
    [ "$code" = 2 ] || die "run should refuse the schema change with exit code 2, got $code"
    [ "$(services)" = "$OLD_SERVICES" ] || die "the refused run changed the layout"
    ok "refused (exit 2); the old layout is still running"
    [ "$(docker inspect -f '{{.Image}}' "$(dc ps -q panel)")" = "$OLD_PANEL_IMAGE" ] || die "the panel container was replaced by the refused run"
else
    say "5. (this upgrade has no database migration: nothing to refuse)"
fi

say "6. run"
upgrade run --with-migrations || die "upgrade.sh run failed"
NEW_SERVICES=$(services)
for old in worker worker-periodic beat redis; do
    case " $NEW_SERVICES " in *" $old "*) continue ;; esac
    [ -z "$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=$old")" ] || die "a $old container is left"
done
[ "$NEW_SERVICES" != "$OLD_SERVICES" ] || die "the layout did not change"
ok "new layout: $NEW_SERVICES (was: $OLD_SERVICES)"
[ "$(stat -c %a "$DEPLOY/upgrade-state.env")" = 600 ] || die "upgrade-state.env is not mode 600"
for file in "$DEPLOY"/upgrade-backup-*.sql "$DEPLOY"/upgrade-backup-*.env; do
    [ "$(stat -c %a "$file")" = 600 ] || die "$file is not mode 600"
done
grep -q "accounts_user" "$DEPLOY"/upgrade-backup-*.sql || die "the database dump looks empty"
! grep -q "^COPY public.django_session .*$USERNAME" "$DEPLOY"/upgrade-backup-*.sql || die "the dump holds login sessions"
[ -z "$(g status --porcelain)" ] || die "git status is not clean: $(g status --porcelain)"
ok "git sees none of the script's files (state, dump and .env copy are ignored)"
data_is_there "new layout"
upgrade status || die "status failed after the upgrade"

say "6b. Redis as its own container and back, without a build (run --no-build): the rollback point stays"
point=$(grep -E '^(FROM_REV|IMAGE)=' "$DEPLOY/upgrade-state.env")
printf 'COMPOSE_PROFILES=external-redis\nMCPANEL_REDIS_URL=redis://redis:6379/0\n' >>"$DEPLOY/.env"
upgrade run --no-build || die "switching to the external Redis failed"
# shellcheck disable=SC2086 # a space separated list
expect_services "$(printf '%s\n' $NEW_SERVICES redis | sort | tr '\n' ' ')"
processes=$(dc exec -T panel python /usr/local/bin/mcpanel-supervise.py status)
case "$processes" in *"  redis "*) die "the panel still runs its own Redis" ;; esac
data_is_there "Redis as its own container"
sed -i -e '/^COMPOSE_PROFILES=external-redis$/d' -e '/^MCPANEL_REDIS_URL=redis:\/\/redis:6379\/0$/d' "$DEPLOY/.env"
upgrade run --no-build || die "switching back to Redis inside the panel container failed"
expect_services "$NEW_SERVICES"
[ -z "$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=redis")" ] || die "the redis container is left"
[ "$(grep -E '^(FROM_REV|IMAGE)=' "$DEPLOY/upgrade-state.env")" = "$point" ] || die "run --no-build changed the rollback point"
ok "back to $NEW_SERVICES, no redis container, the rollback point is unchanged"
data_is_there "Redis inside the panel container again"

say "7. rollback"
upgrade rollback || die "upgrade.sh rollback failed"
expect_services "$OLD_SERVICES"
[ "$(g rev-parse HEAD)" = "$OLD_REV" ] || die "the source tree is not at the old revision"
ok "source tree back at $(g log -1 --format='%h')"
[ "$(docker inspect -f '{{.Image}}' "$(dc ps -q panel)")" = "$OLD_PANEL_IMAGE" ] || die "the panel does not run the saved image"
ok "the panel runs the saved image again"
if [ "$MIGRATES" = 1 ]; then
    pending=$(dc exec -T panel python manage.py showmigrations --plan | grep -c '\[ \]')
    applied=$(sql "SELECT count(*) FROM django_migrations")
    ok "database schema is the old one again ($applied migrations applied, $pending unapplied in the old code)"
    [ "$pending" = 0 ] || die "the old code sees unapplied migrations"
fi
data_is_there "after the rollback"

say "8. upgrade again"
g checkout -q "$BRANCH" || die "cannot switch back to $BRANCH"
upgrade run --with-migrations || die "the second upgrade failed"
expect_services "$NEW_SERVICES"
data_is_there "new layout, second time"
tags=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep -c '^mcpanel-rollback/')
ok "rollback images kept: $tags (one generation)"

say "REHEARSAL PASSED"
