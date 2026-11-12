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
#   -> when the old revision is from before multi-game: a Factorio server and a Minecraft server
#      that share a port number (rows only, no game container); rollback and its dry run refuse
#      and list them
#   -> rollback (with --archive-other-games in that case): it dumps the database first; then the
#      old layout and the old images again, schema changes undone, data still there, the Factorio
#      server archived, recorded and parked: Unarchive + Start in the old panel fails, no container.
#      While its `docker compose up` replaces the containers the terminal is closed (SIGHUP to the
#      whole process group): the rollback still finishes
#   -> run again, with the terminal closed in the same way: the switch still finishes, and the
#      Factorio server is a Factorio server again (archived, its settings and secret back)
#   -> the way back without undoing migrations: restore-db with the dump of that run, then rollback
#   -> restore-db once more, after that finished rollback: the commands it prints start the old
#      layout again
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
# The fast-forward and checkouts below write files with this umask: upgrade.sh refuses a checkout
# whose files group or others can write (security review 2026-11-11, D-2).
umask 022

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

# upgrade.sh the way it runs in a terminal that is then closed: in a session of its own, and when
# its output shows that `docker compose up` has begun to replace the containers, the whole process
# group gets SIGHUP. It must finish all the same: prints its output, returns its exit status.
#   upgrade_in_a_terminal_that_closes WHAT-up-PRINTS-LAST ARGS...
upgrade_in_a_terminal_that_closes() {
    local last="$1" out="$SHIM/terminal.out" wrapper deadline=$((SECONDS + 1200)) code after left
    shift
    : >"$out"
    # The wrapper only catches the signal, to report the exit status: a signal that is caught is
    # back at its default in upgrade.sh (one that is ignored would stay ignored there).
    # shellcheck disable=SC2016 # expanded by the wrapper
    setsid bash -c 'trap : HUP; cd "$0" && ./scripts/upgrade.sh "$@"; echo "### upgrade.sh exit=$?"' "$DEPLOY" "$@" >>"$out" 2>&1 &
    wrapper=$!
    until grep -q -E 'Container [^ ]+-agent-1 +Recreated' "$out"; do
        if ! kill -0 "$wrapper" 2>/dev/null; then
            cat "$out"
            die "upgrade.sh $* ended before docker compose up replaced a container"
        fi
        [ "$SECONDS" -lt "$deadline" ] || die "upgrade.sh $* did not get to docker compose up"
        sleep 0.05
    done
    kill -HUP -- "-$wrapper" || die "could not signal the process group of upgrade.sh"
    echo "### SIGHUP sent to the process group $wrapper" >>"$out"
    wait "$wrapper"
    cat "$out"
    # the signal came while `docker compose up` was at work: its last lines were written after it
    after=$(sed -n '/^### SIGHUP sent/,$p' "$out")
    grep -q -E "$last" <<<"$after" || die "the signal came after docker compose up had ended: nothing was tried"
    code=$(sed -n 's/^### upgrade.sh exit=//p' "$out" | tail -n 1)
    [ -n "$code" ] || die "the wrapper of upgrade.sh did not report an exit status"
    # no container under the temporary name of a replacement, none that was made and not started
    left=$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}={{.State}}' | grep -E '^[0-9a-f]{12}_|=(created|exited|dead)$' | tr '\n' ' ')
    [ -z "$left" ] || die "containers are left half replaced or not started: $left"
    return "$code"
}
short() { git -C "$SITE" rev-parse --short "$1"; }
# The services deploy/docker-compose.yml of revision REV defines (with today's .env and profiles),
# sorted, as `services` prints them. The file is read from git, not checked out.
compose_services_of() { # compose_services_of REV
    local file="$SHIM/compose-$1.yml"
    g show "$1:deploy/docker-compose.yml" >"$file" 2>/dev/null || return 1
    (cd "$DEPLOY" && docker compose --project-directory "$DEPLOY" -f "$file" config --services) | sort | tr '\n' ' '
}
newest_log() { find "$DEPLOY" -maxdepth 1 -name 'upgrade-log-*.log' -printf '%T@ %p\n' | sort -n | tail -n 1 | cut -d' ' -f2-; }

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
# Python in the running panel (Django shell); its last line of output
panel_py() { dc exec -T panel python manage.py shell -v 0 -c "$1" </dev/null | tail -n 1; }
csrf() { grep mcpanel_csrftoken "$JAR" | tr '\t' ' ' | rev | cut -d' ' -f1 | rev; }
# The session token of the last login or setup (security review D-1, interface I-7): a panel that
# binds its sessions answers it with the login and wants it as X-MCPanel-Session-Token on every
# /api/ call; an older panel answers none and ignores the header. Empty until a login.
SESSION_TOKEN=""
api() { # api METHOD PATH [JSON] -> HTTP status; body in $OUT
    local method="$1" path="$2" body="${3:-}"
    local -a bound=()
    [ -z "$SESSION_TOKEN" ] || bound=(-H "X-MCPanel-Session-Token: $SESSION_TOKEN")
    curl -sS -o "$OUT" -w '%{http_code}' -b "$JAR" -c "$JAR" -X "$method" ${bound[@]+"${bound[@]}"} \
        -H "X-CSRFToken: $(csrf)" -H "Origin: $URL" -H "Referer: $URL/" -H 'Content-Type: application/json' \
        ${body:+-d "$body"} "$URL$path"
}
# After a login or setup: its answer ($OUT) holds the token (43 URL-safe base64 characters). Called
# in this shell, not inside $(api ...), which runs in a subshell.
remember_session_token() {
    SESSION_TOKEN=$(sed -n 's/.*"session_token": *"\([A-Za-z0-9_-]*\)".*/\1/p' "$OUT" | head -n 1)
}
login() {
    rm -f "$JAR"
    SESSION_TOKEN=""
    curl -fsS -c "$JAR" -o /dev/null "$URL/api/auth/state" || return 1
    [ "$(api POST /api/auth/login "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\"}")" = 200 ] || return 1
    remember_session_token
}
data_is_there() { # the account logs in and the changed setting has its value
    local kept status
    login || die "$1: the owner account cannot log in"
    [ "$(api GET /api/system/settings)" = 200 ] || die "$1: /api/system/settings answered $(cat "$OUT")"
    grep -q '"default_memory_mb": *3072' "$OUT" || die "$1: the changed setting is gone: $(cat "$OUT")"
    if [ -n "$SESSION_TOKEN" ]; then # a panel that binds sessions: the cookie alone is not enough
        kept=$SESSION_TOKEN
        SESSION_TOKEN=""
        status=$(api GET /api/system/settings)
        SESSION_TOKEN=$kept
        [ "$status" = 401 ] && grep -q session_token_required "$OUT" ||
            die "$1: a call with the session cookie only answered $status: $(cat "$OUT")"
        ok "$1: the session cookie alone is refused (session_token_required)"
    fi
    [ "$(sql "SELECT count(*) FROM accounts_user")" = 1 ] || die "$1: expected exactly one user"
    ok "$1: the owner account logs in, the changed setting is there, $(sql "SELECT count(*) FROM audit_auditevent") audit entries"
}
nothing_was_recorded() { # a refused run left no trace
    [ "$(services)" = "$OLD_SERVICES" ] || die "$1: the refused run changed the layout"
    [ ! -e "$STATE" ] || die "$1: the refused run wrote a state file"
    [ -z "$(find "$DEPLOY" -maxdepth 1 -name 'upgrade-backup-*')" ] || die "$1: the refused run dumped the database"
    [ -z "$(find "$DEPLOY" -maxdepth 1 -name 'upgrade-log-*')" ] || die "$1: the refused run wrote a log"
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
remember_session_token
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
# What the compose files define (with the profiles of .env): the old revision's, read from git, and
# the new one's. The layout changes only when they differ (from before 2026-11-08 the old revision
# has the 7-container layout; a newer one, e.g. 082b949, has panel, agent and postgres already).
OLD_DEFINED=$(compose_services_of "$OLD_REV") || die "cannot read the services of deploy/docker-compose.yml at $(short "$OLD_REV")"
NEW_DEFINED=$(dc config --services | sort | tr '\n' ' ')
[ "$OLD_DEFINED" = "$OLD_SERVICES" ] || die "the old compose file defines '$OLD_DEFINED', but '$OLD_SERVICES' ran"
[ "$NEW_SERVICES" = "$NEW_DEFINED" ] || die "running services are '$NEW_SERVICES', the new compose file defines '$NEW_DEFINED'"
if [ "$NEW_DEFINED" != "$OLD_DEFINED" ]; then
    ok "new layout: $NEW_SERVICES (was: $OLD_SERVICES)"
else
    ok "same layout as the old revision's compose file: $NEW_SERVICES"
fi
[ "$(stat -c %a "$STATE")" = 600 ] || die "upgrade-state.env is not mode 600"
for file in "$DEPLOY"/upgrade-backup-*.sql "$DEPLOY"/upgrade-backup-*.env "$DEPLOY"/upgrade-log-*.log; do
    [ "$(stat -c %a "$file")" = 600 ] || die "$file is not mode 600"
done
log=$(newest_log)
grep -q '^==> Done$' "$log" || die "the log of the resumed run ($log) does not say how it ended"
tail -n 1 "$log" | grep -q 'exit status 0$' || die "the last line of $log is not the exit status"
grep -q "accounts_user" "$DEPLOY"/upgrade-backup-*.sql || die "the database dump looks empty"
! grep -q "^COPY public.django_session .*$USERNAME" "$DEPLOY"/upgrade-backup-*.sql || die "the dump holds login sessions"
[ -z "$(g status --porcelain)" ] || die "git status is not clean: $(g status --porcelain)"
ok "git sees none of the script's files (state, dump and .env copy are ignored)"
data_is_there "new layout"
status=$(upgrade status) || die "status failed after the upgrade"
printf '%s\n' "$status"
case "$status" in
    *"Log:          $log"*"exit status 0"*) ok "status names the log of the last run and how it ended" ;;
    *) die "status does not name the log $log with its exit status" ;;
esac

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

# A rollback to a release from before multi-game (no game column) must not lose other games.
GAMES=0
ROLLBACK_FLAGS=""
if ! g cat-file -e "$OLD_REV:backend/apps/instances/migrations/0005_instance_game.py" 2>/dev/null &&
    g cat-file -e "HEAD:backend/apps/instances/migrations/0007_restore_other_games.py" 2>/dev/null; then
    GAMES=1
    ROLLBACK_FLAGS="--archive-other-games"
fi
FACTORIO=rehearsal-factorio
PARKED="parked by upgrade.sh rollback (other games)" # the node undoing migration instances 0007 parks them on
factorio_row() { sql "SELECT game_port, archived_at IS NOT NULL FROM instances_instance WHERE name = '$FACTORIO'"; }
factorio_schedule_on() { sql "SELECT bool_or(s.enabled) FROM instances_schedule s JOIN instances_instance i ON i.id = s.instance_id WHERE i.name = '$FACTORIO' AND s.name = 'rehearsal'"; }
if [ "$GAMES" = 1 ]; then
    say "6c. a Factorio server and a Minecraft server that share port number 34197 (rows only, no game container)"
    created=$(panel_py "
from apps.instances.models import Instance, Schedule
from apps.nodes.models import Node
node = Node.default()
Instance.objects.create(name='rehearsal-mc', node=node, image='itzg/minecraft-server:stable', game_port=34197)
fa = Instance.objects.create(name='$FACTORIO', node=node, game='factorio', server_type='', minecraft_version='2.0.55',
    image='factoriotools/factorio:2.0.55-rootless', memory_mb=2048, jvm_flags_preset='none', game_port=34197,
    ports=[{'name': 'game', 'container_port': 34197, 'protocol': 'udp', 'host_port': 34197}],
    game_options={'space_age': False, 'save_name': 'world'}, setup_state={'steps': {}})
fa.set_secret_values({'token': 'rehearsal-secret'})
fa.save(update_fields=['secrets'])
Schedule.objects.create(instance=fa, name='rehearsal', action='backup', cron='0 4 * * *')
print('created', fa.pk)")
    case "$created" in created\ *) ok "Factorio server ${created#created }, Minecraft server on the same number" ;; *) die "creating the servers failed: $created" ;; esac

    say "6d. rollback and its dry run refuse while a server of another game exists"
    for dry in --dry-run ""; do
        # shellcheck disable=SC2086 # empty or one option
        refusal=$(upgrade rollback $dry 2>&1)
        code=$?
        printf '%s\n' "$refusal"
        [ "$code" = 2 ] || die "rollback $dry should refuse (exit 2) while a Factorio server exists, got $code"
        case "$refusal" in
            *"'$FACTORIO'  factorio"*"--archive-other-games"*) ;;
            *) die "the refusal does not list the Factorio server and name --archive-other-games" ;;
        esac
        case "$refusal" in *"'$FACTORIO' (game port 34197) gets 65536"*) ;; *) die "the refusal does not name the shared port number" ;; esac
    done
    expect_services "$NEW_SERVICES"
    [ "$(state STATUS)" = "done" ] || die "the refused rollback changed the state to '$(state STATUS)'"
    [ "$(factorio_row)" = "34197|f" ] || die "the refused rollback changed the Factorio server: $(factorio_row)"
    ok "refused (exit 2), listed the server and the shared number; nothing was changed"
fi

say "7. rollback${ROLLBACK_FLAGS:+ $ROLLBACK_FLAGS}; the terminal is closed while docker compose up brings the old layout back"
# shellcheck disable=SC2086 # empty or one option
upgrade_in_a_terminal_that_closes 'Container [^ ]+-panel-1 +(Started|Healthy)' rollback $ROLLBACK_FLAGS || die "upgrade.sh rollback did not finish after its terminal was closed"
[ "$(state STATUS)" = rolled-back ] || die "the state should be 'rolled-back', it is '$(state STATUS)'"
tail -n 1 "$(newest_log)" | grep -q 'exit status 0$' || die "the log of the rollback does not end with the exit status 0"
ok "the rollback finished without its terminal (the log says so: $(newest_log))"
rollback_dump=$(state ROLLBACK_SQL)
[ -f "$rollback_dump" ] && [ "$(stat -c %a "$rollback_dump")" = 600 ] || die "the rollback took no dump of the database (mode 600) first: '$rollback_dump'"
grep -q "accounts_user" "$rollback_dump" || die "the dump of the rollback looks empty"
ok "the rollback dumped the database before it changed anything: $rollback_dump"
the_old_version_runs "rollback"
if [ "$MIGRATES" = 1 ]; then
    pending=$(dc exec -T panel python manage.py showmigrations --plan | grep -c '\[ \]')
    applied=$(sql "SELECT count(*) FROM django_migrations")
    ok "database schema is the old one again ($applied migrations applied, $pending unapplied in the old code)"
    [ "$pending" = 0 ] || die "the old code sees unapplied migrations"
fi
data_is_there "after the rollback"
if [ "$GAMES" = 1 ]; then
    [ "$(sql "SELECT count(*) FROM information_schema.columns WHERE table_name = 'instances_instance' AND column_name = 'game'")" = 0 ] || die "the game column should be gone after the rollback"
    [ "$(factorio_row)" = "65536|t" ] || die "the Factorio server should be archived with the rollback port 65536: $(factorio_row)"
    [ "$(sql "SELECT game FROM mcpanel_rollback_other_games")" = factorio ] || die "the Factorio server is not recorded"
    [ "$(factorio_schedule_on)" = f ] || die "the schedule of the Factorio server should be disabled"
    ok "the Factorio server is archived (game port 65536 while the old version runs), recorded, its schedule off"
    parked=$(sql "SELECT n.name || '|' || n.agent_url || '|' || n.is_default FROM instances_instance i JOIN nodes_node n ON n.id = i.node_id WHERE i.name = '$FACTORIO'")
    case "$parked" in
        "$PARKED|http://127.0.0.1:1/"*"|false") ok "the Factorio server is parked: $parked" ;;
        *) die "the Factorio server should be on the parked node that cannot be reached: $parked" ;;
    esac
    # The mistake of the gate run of 2026-11-10: in the old panel, Unarchive and then Start ran the
    # Minecraft image in the folder of a server of another game. Parked, the start fails at once.
    id=$(sql "SELECT id FROM instances_instance WHERE name = '$FACTORIO'")
    [ "$(api POST "/api/instances/$id/unarchive")" = 200 ] || die "the old panel did not unarchive the Factorio server: $(cat "$OUT")"
    [ "$(api POST "/api/instances/$id/power" '{"action":"start"}')" = 202 ] || die "the old panel refused the start request: $(cat "$OUT")"
    job=$(sed -n 's/^{"id":"\([0-9a-f-]*\)".*/\1/p' "$OUT")
    [ -n "$job" ] || die "the start request answered no job: $(cat "$OUT")"
    for _ in $(seq 120); do
        [ "$(api GET "/api/jobs/$job")" = 200 ] || die "cannot read job $job: $(cat "$OUT")"
        grep -q -E '"status": *"(queued|running)"' "$OUT" || break
        sleep 0.5
    done
    if ! grep -q -E '"status": *"failed"' "$OUT" || ! grep -q 'parked-by-upgrade-rollback' "$OUT"; then
        die "the start in the old panel should fail on the parked node: $(cat "$OUT")"
    fi
    [ -z "$(docker ps -aq --filter "label=io.mcpanel.instance=$id")" ] || die "the old panel made a container for the Factorio server"
    ok "Unarchive + Start in the old panel: the start fails on the parked node, no container ($(sed -n 's/.*"error": *"\([^"]*\)".*/\1/p' "$OUT" | cut -c1-110))"
fi

say "8. upgrade again; the terminal is closed while docker compose up replaces the containers"
g checkout -q "$BRANCH" || die "cannot switch back to $BRANCH"
upgrade_in_a_terminal_that_closes 'Container [^ ]+-panel-1 +Started' run --with-migrations || die "the second upgrade did not finish after its terminal was closed"
[ "$(state STATUS)" = "done" ] || die "the state should be 'done', it is '$(state STATUS)'"
grep -q '^==> Done$' "$(newest_log)" || die "the log of the run does not say that it finished"
ok "the switch finished without its terminal (the log says so: $(newest_log))"
expect_services "$NEW_SERVICES"
data_is_there "new layout, second time"
if [ "$GAMES" = 1 ]; then
    [ "$(sql "SELECT game || '|' || game_port || '|' || (archived_at IS NOT NULL) || '|' || (ports->0->>'protocol') || '|' || (game_options->>'save_name') FROM instances_instance WHERE name = '$FACTORIO'")" = "factorio|34197|true|udp|world" ] || die "the Factorio server did not come back as it was"
    [ "$(panel_py "from apps.instances.models import Instance; print(Instance.objects.get(name='$FACTORIO').secret_values)")" = "{'token': 'rehearsal-secret'}" ] || die "the secret of the Factorio server did not come back"
    [ "$(factorio_schedule_on)" = t ] || die "the schedule of the Factorio server should be enabled again"
    [ "$(sql "SELECT to_regclass('public.mcpanel_rollback_other_games') IS NULL")" = t ] || die "the side table should be dropped"
    [ "$(sql "SELECT n.is_default FROM instances_instance i JOIN nodes_node n ON n.id = i.node_id WHERE i.name = '$FACTORIO'")" = t ] || die "the Factorio server should be on its own node again"
    [ "$(sql "SELECT count(*) FROM nodes_node WHERE name = '$PARKED'")" = 0 ] || die "the parked node should be gone"
    ok "the Factorio server is back (game, port, ports, options, secret, schedule, its node), still archived; the side table and the parked node are gone"
fi
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
restored=$(upgrade restore-db "$dump" --yes) || die "restore-db failed"
printf '%s\n' "$restored"
case "$restored" in
    *"./scripts/upgrade.sh rollback"*) ok "restore-db names the rollback as the next step (the upgrade is what is deployed)" ;;
    *) die "restore-db does not name the rollback as the next step" ;;
esac
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

say "10. restore-db after that finished rollback: the commands it prints start the old layout again"
# The source tree is the new version again (the script is back), the old version is deployed.
refusal=$(upgrade restore-db "$dump" --yes 2>&1)
code=$?
[ "$code" = 2 ] || die "restore-db should refuse (exit 2) while the old layout uses the database, got $code"
stop=$(sed -n 's/^ *\(docker compose stop [a-z -]*\)$/\1/p' <<<"$refusal")
[ -n "$stop" ] || die "restore-db does not print the command that stops the other services: $refusal"
printf '$ %s\n' "$stop"
(cd "$DEPLOY" && bash -c "$stop") >/dev/null 2>&1 || die "the command restore-db printed failed: $stop"
restored=$(upgrade restore-db "$dump" --yes) || die "restore-db after the rollback failed"
printf '%s\n' "$restored"
case "$restored" in *"upgrade.sh rollback"*) die "restore-db names a rollback although the last upgrade is rolled back already" ;; esac
next=$(sed -n 's/^ \{6\}\(cd .* && docker compose up -d\)$/\1/p' <<<"$restored")
[ -n "$next" ] || die "restore-db does not print the command that starts the stack again"
case "$next" in
    *"git checkout --detach $OLD_REV && cd deploy && docker compose up -d") ;;
    *) die "the command restore-db prints does not check out the deployed revision first: $next" ;;
esac
printf '\n$ %s\n' "$next"
bash -c "$next" || die "the command restore-db printed failed"
dc up -d --wait --wait-timeout 300 >/dev/null 2>&1 || die "the old layout is not healthy after the command restore-db printed"
the_old_version_runs "restore-db after the rollback, then the command it printed"
data_is_there "after restore-db and the command it printed"
[ "$(sql "SELECT count(*) FROM pg_database WHERE datname LIKE '%before_restore%'")" = 2 ] || die "both earlier databases should be kept under other names"
g checkout -q "$BRANCH" || die "cannot switch back to $BRANCH"

say "REHEARSAL PASSED"
