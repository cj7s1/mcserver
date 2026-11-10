#!/bin/bash
# Rehearsal of deploy/scripts/upgrade.sh with real containers (CI job "upgrade"; also by hand):
#
#   the layout of an older revision, with an owner account and a changed setting
#   -> status and dry-run change nothing
#   -> run refuses while a job runs, and refuses a schema change without --with-migrations
#      (before it records, dumps, pulls or builds anything)
#   -> a run whose new version is not healthy in time does not finish: a second run refuses and
#      leaves the rollback point alone, rollback returns to the old images
#   -> a run whose `docker compose up` fails after the old workers were removed does not finish:
#      a second run refuses, `run --resume` finishes it with the rollback point of the old version
#   -> the new layout, the account and the setting are still there, no old container left
#   -> rollback: the old layout and the old images again, schema changes undone, data still there
#   -> run again
#   -> the way back without undoing migrations: restore-db with the dump of that run, then rollback
#
#   upgrade-rehearsal.sh SITE NEW_REV [URL]
#
# SITE     a git checkout of the OLD revision on a branch, with deploy/.env in place (and, if
#          wanted, deploy/docker-compose.override.yml). Its stack must not be running yet.
# NEW_REV  the revision to upgrade to; SITE's branch is fast-forwarded to it, like `git pull`.
# URL      where the panel answers (default http://127.0.0.1:8080).
#
# The same sequences against a stand-in for docker: backend/tests/test_upgrade_script.py.
set -uo pipefail

SITE=${1:?usage: upgrade-rehearsal.sh SITE NEW_REV [URL]}
NEW_REV=${2:?usage: upgrade-rehearsal.sh SITE NEW_REV [URL]}
URL=${3:-http://127.0.0.1:8080}
DEPLOY="$SITE/deploy"
STATE="$DEPLOY/upgrade-state.env"
USERNAME=rehearsal-owner
PASSWORD='Rehearsal-Horse-Battery-42'
JAR=$(mktemp)
OUT=$(mktemp)
SHIM=$(mktemp -d)
trap 'rm -rf "$JAR" "$OUT" "$SHIM"' EXIT

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

# upgrade.sh with a `docker` whose next `docker compose up` fails, once. Everything else, and every
# later `up`, is the real docker.
upgrade_with_a_failing_up() {
    local real
    real=$(command -v docker) || die "docker is not installed"
    cat >"$SHIM/docker" <<'WRAPPER'
#!/bin/bash
if [ -e "$FAIL_NEXT_UP" ]; then
    case " $* " in
        *" compose up "*)
            rm -f "$FAIL_NEXT_UP"
            echo "rehearsal: docker compose up fails this once" >&2
            exit 1
            ;;
    esac
fi
exec "$REAL_DOCKER" "$@"
WRAPPER
    chmod +x "$SHIM/docker"
    touch "$SHIM/fail-next-up"
    (cd "$DEPLOY" && PATH="$SHIM:$PATH" FAIL_NEXT_UP="$SHIM/fail-next-up" REAL_DOCKER="$real" ./scripts/upgrade.sh "$@")
}

state() { sed -n "s/^$1=//p" "$STATE" | head -n 1; }
rollback_point() { grep -E '^(FROM_REV|IMAGE|MIGRATION)=' "$STATE"; }
services() { dc ps --services --status running | sort | tr '\n' ' '; }
containers() { docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.ID}} {{.Label "com.docker.compose.service"}}' | sort -k2; }
panel_image() { docker inspect -f '{{.Image}}' "$(dc ps -q panel)"; }
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
nothing_was_recorded() { # a refused run left no trace
    [ "$(services)" = "$OLD_SERVICES" ] || die "$1: the refused run changed the layout"
    [ ! -e "$STATE" ] || die "$1: the refused run wrote a state file"
    [ -z "$(find "$DEPLOY" -maxdepth 1 -name 'upgrade-backup-*')" ] || die "$1: the refused run dumped the database"
    [ "$(docker image inspect -f '{{.Id}}' "$PANEL_IMAGE_NAME")" = "$OLD_PANEL_IMAGE" ] || die "$1: $PANEL_IMAGE_NAME no longer is the old image"
    [ "$(panel_image)" = "$OLD_PANEL_IMAGE" ] || die "$1: the panel container was replaced"
}
rollback_point_is_the_old_version() {
    [ "$(state FROM_REV)" = "$OLD_REV" ] || die "$1: the rollback point is revision '$(state FROM_REV)', not the old one ($OLD_REV)"
    grep -q -F "IMAGE=$PANEL_IMAGE_NAME|$OLD_PANEL_IMAGE|" "$STATE" || die "$1: the rollback point does not hold the old panel image"
    ok "$1: the rollback point is the old revision with the old panel image"
}
the_old_version_runs() {
    expect_services "$OLD_SERVICES"
    [ "$(g rev-parse HEAD)" = "$OLD_REV" ] || die "$1: the source tree is not at the old revision"
    [ "$(panel_image)" = "$OLD_PANEL_IMAGE" ] || die "$1: the panel does not run the saved image"
    ok "$1: source tree back at $(g log -1 --format='%h'), the panel runs the saved image again"
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
OLD_PANEL_IMAGE=$(panel_image)
PANEL_IMAGE_NAME=$(docker inspect -f '{{.Config.Image}}' "$(dc ps -q panel)")
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
RUN_FLAGS="" # what `run` needs for this upgrade: empty, or --with-migrations
plan=$(upgrade dry-run 2>/dev/null) # (not piped into grep -q: with pipefail an early exit of grep fails the pipe)
case "$plan" in
    *"database migrations in this version"*)
        MIGRATES=1
        RUN_FLAGS="--with-migrations"
        ;;
esac
case "$plan" in
    *"To do it: ./scripts/upgrade.sh run${RUN_FLAGS:+ $RUN_FLAGS}") ok "the dry run ends with the command that works: run${RUN_FLAGS:+ $RUN_FLAGS}" ;;
    *) die "the dry run does not end with the full command (./scripts/upgrade.sh run${RUN_FLAGS:+ $RUN_FLAGS})" ;;
esac

say "4. run refuses while a job is running"
job=$(sql "INSERT INTO jobs_job (id, kind, title, status, exclusive, progress, message, error, error_code, params, result, log, agent_operation_id, created_at, started_at, heartbeat_at) VALUES (gen_random_uuid(), 'backup', 'rehearsal', 'running', false, 0, '', '', '', '{}', '{}', '[]', '', now(), now(), now()) RETURNING id" | head -n 1)
# shellcheck disable=SC2086 # RUN_FLAGS is empty or one option
upgrade run $RUN_FLAGS
code=$?
[ "$code" = 2 ] || die "run should refuse with exit code 2 while a job runs, got $code"
nothing_was_recorded "job running"
ok "refused (exit 2); nothing was recorded, dumped, built or switched"
sql "DELETE FROM jobs_job WHERE id = '$job'" >/dev/null

if [ "$MIGRATES" = 1 ]; then
    say "5. run refuses a database schema change without --with-migrations, before it does anything"
    refusal=$(upgrade run 2>&1)
    code=$?
    printf '%s\n' "$refusal"
    [ "$code" = 2 ] || die "run should refuse the schema change with exit code 2, got $code"
    nothing_was_recorded "schema change"
    case "$refusal" in
        *"./scripts/upgrade.sh run --with-migrations"*) ;;
        *) die "the refusal does not name the full command" ;;
    esac
    ok "refused (exit 2) with the full command; no state file, no dump, image names and the old layout untouched"
else
    say "5. (this upgrade has no database migration: nothing to refuse)"
fi

say "5a. the new version is not healthy in time: the run is not finished, a second run refuses, rollback goes back"
# shellcheck disable=SC2086
upgrade run $RUN_FLAGS --timeout 0
code=$?
[ "$code" = 1 ] || die "a run whose services cannot be healthy within 0 s should end with exit code 1, got $code"
[ "$(state STATUS)" = switching ] || die "the state should be 'switching', it is '$(state STATUS)'"
rollback_point_is_the_old_version "unhealthy run"
point=$(rollback_point)
# shellcheck disable=SC2086
upgrade run $RUN_FLAGS
code=$?
[ "$code" = 2 ] || die "a run on top of a run that did not finish should refuse with exit code 2, got $code"
[ "$(rollback_point)" = "$point" ] || die "the refused run changed the rollback point"
ok "the second run refused (exit 2) and left the rollback point alone"
upgrade rollback || die "rollback of the run that did not finish failed"
the_old_version_runs "rollback of the unhealthy run"
data_is_there "after the rollback of the unhealthy run"
g checkout -q "$BRANCH" || die "cannot switch back to $BRANCH"

say "5b. docker compose up fails after the old workers were removed: a second run refuses, run --resume finishes it"
# shellcheck disable=SC2086
upgrade_with_a_failing_up run $RUN_FLAGS
code=$?
[ "$code" = 1 ] || die "a run whose docker compose up fails should end with exit code 1, got $code"
[ ! -e "$SHIM/fail-next-up" ] || die "docker compose up was never reached"
[ "$(state STATUS)" = switching ] || die "the state should be 'switching', it is '$(state STATUS)'"
case " $OLD_SERVICES " in
    *" worker "*) [ -z "$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=worker")" ] || die "the old worker should be gone at this point" ;;
esac
rollback_point_is_the_old_version "failed compose up"
point=$(rollback_point)
# shellcheck disable=SC2086
upgrade run $RUN_FLAGS
code=$?
[ "$code" = 2 ] || die "a run on top of a run that did not finish should refuse with exit code 2, got $code"
[ "$(rollback_point)" = "$point" ] || die "the refused run changed the rollback point"
ok "the second run refused (exit 2) and left the rollback point alone"

say "6. run --resume finishes the upgrade"
upgrade run --resume || die "upgrade.sh run --resume failed"
[ "$(state STATUS)" = "done" ] || die "the state should be 'done', it is '$(state STATUS)'"
rollback_point_is_the_old_version "resumed run"
NEW_SERVICES=$(services)
for old in worker worker-periodic beat redis; do
    case " $NEW_SERVICES " in *" $old "*) continue ;; esac
    [ -z "$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=$old")" ] || die "a $old container is left"
done
[ "$NEW_SERVICES" != "$OLD_SERVICES" ] || die "the layout did not change"
ok "new layout: $NEW_SERVICES (was: $OLD_SERVICES)"
[ "$(stat -c %a "$STATE")" = 600 ] || die "upgrade-state.env is not mode 600"
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
point=$(rollback_point)
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
[ "$(rollback_point)" = "$point" ] || die "run --no-build changed the rollback point"
ok "back to $NEW_SERVICES, no redis container, the rollback point is unchanged"
data_is_there "Redis inside the panel container again"

say "7. rollback"
upgrade rollback || die "upgrade.sh rollback failed"
the_old_version_runs "rollback"
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

say "9. the way back without undoing migrations: restore-db with the dump of that run, then rollback"
dump=$(state BACKUP_SQL)
[ -f "$dump" ] || die "the state file names no database dump"
applied_new=$(sql "SELECT count(*) FROM django_migrations")
upgrade restore-db "$dump" --yes
code=$?
[ "$code" = 2 ] || die "restore-db should refuse (exit 2) while the panel uses the database, got $code"
others=$(dc ps --services --status running | grep -v '^postgres$' | tr '\n' ' ')
# shellcheck disable=SC2086 # a space separated list
dc stop $others >/dev/null 2>&1 || die "stopping $others failed"
upgrade restore-db "$dump"
code=$?
[ "$code" = 2 ] || die "restore-db without --yes should only say what it would do (exit 2), got $code"
[ "$(sql "SELECT count(*) FROM pg_database WHERE datname LIKE '%before_restore%'")" = 0 ] || die "restore-db without --yes changed the databases"
upgrade restore-db "$dump" --yes || die "restore-db failed"
[ "$(sql "SELECT count(*) FROM pg_database WHERE datname LIKE '%before_restore%'")" = 1 ] || die "the database as it was should be kept under another name"
applied_old=$(sql "SELECT count(*) FROM django_migrations")
if [ "$MIGRATES" = 1 ]; then
    [ "$applied_old" -lt "$applied_new" ] || die "the restored database should have the schema from before the upgrade ($applied_old migrations, the upgraded one had $applied_new)"
fi
[ "$(sql "SELECT count(*) FROM accounts_user")" = 1 ] || die "the restored database should hold the owner account"
ok "the dump is the database now ($applied_old migrations applied, the upgraded one had $applied_new); the database as it was is kept"
upgrade rollback || die "rollback after restore-db failed"
the_old_version_runs "rollback after restore-db"
data_is_there "after restore-db and rollback"
g checkout -q "$BRANCH" || die "cannot switch back to $BRANCH"

say "REHEARSAL PASSED"
