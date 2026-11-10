#!/bin/bash
# Upgrade the MCPanel stack of this checkout, or take the last upgrade back.
#
#   cd <mcpanel source>/deploy
#   ./scripts/upgrade.sh status      what runs now, what the source tree wants, the rollback point
#   ./scripts/upgrade.sh dry-run     everything `run` would do; changes nothing
#   ./scripts/upgrade.sh run         build the version of the source tree and switch to it
#   ./scripts/upgrade.sh rollback    back to the version and images from before the last `run`
#   ./scripts/upgrade.sh restore-db FILE
#                                    put a database dump of an earlier `run` back (asks for --yes)
#
# The usual upgrade:
#
#   cd <mcpanel source> && git pull
#   cd deploy && ./scripts/upgrade.sh dry-run
#   ./scripts/upgrade.sh run        (the dry run ends with the exact command: with --with-migrations
#                                    when the new version changes the database schema)
#
# What `run` does, in this order:
#   1. refuses a version that changes the database schema unless --with-migrations was given
#      (it compares the migration files of the two revisions, before anything is recorded, dumped,
#      pulled or built), and refuses while a job is queued or running (asks the database; --force
#      skips this)
#   2. records the rollback point: the git revision that is deployed, the image of every
#      container of the stack (tagged mcpanel-rollback/<name>:<time>) and the state of the
#      database migrations. A plain `docker image prune` keeps a tagged image, but
#      `docker image prune -a`, `docker system prune -a` and Unraid's removal of orphan images
#      delete it as soon as no container uses it. `status` says when an image of the rollback
#      point is gone.
#   3. dumps the database (without login sessions) and copies .env next to it, both mode 600
#   4. pulls the upstream images (database) and rebuilds panel and agent on fresh base images
#   5. asks the new image whether it changes the database schema, and refuses here if it does
#      although step 1 saw no new migration file (unless --with-migrations)
#   6. stops and removes containers of services the new version no longer has (for example
#      worker, worker-periodic, beat, redis) with 2 minutes to finish, then
#      `docker compose up -d --remove-orphans`
#   7. waits until every service is running and healthy, checks that nothing of the old layout
#      is left and prints what changed
# Minecraft server containers are never touched: they do not belong to the Compose project.
# Until step 6 nothing is switched: a run that ends before it points the image names (for example
# mcpanel/panel:local) back at the images they had, and the old version keeps running.
#
# A `run` that did not finish (the build failed, the new version did not become healthy, the
# terminal session was closed) keeps its rollback point, and no later `run` makes a new one on
# top of it. If nothing was switched yet, the next `run` goes on with the recorded one. Otherwise
# `run` refuses until you choose: `rollback` (back to the old version) or `run --resume` (go on
# to the new one, with the rollback point as it was recorded).
#
# `rollback` checks out the recorded revision (detached HEAD), points the image names back at
# the saved images, undoes the database migrations of the upgrade and brings that layout up.
# It asks the upgraded version first whether every migration can be undone and stops nothing if
# one cannot. After the checkout this script may be gone from disk (older revisions do not have
# it); `git checkout <branch>` brings it back.
#
# `restore-db FILE` loads a dump written by step 3 into a new database and, when that worked,
# swaps it with the current one, which is kept under another name. Everything but the database
# container must be stopped. It is the way back when a migration cannot be undone.
#
# Options:
#   --force             run, rollback: do not refuse because of queued or running jobs
#   --with-migrations   run: the new version may change the database schema
#   --resume            run: continue the run that did not finish, with its rollback point
#   --from REV          run, dry-run: the git revision that is deployed now, if the script cannot tell
#   --no-pull           run: build with the base images already on this host (no internet needed)
#   --no-build          run, dry-run: only apply a change of .env or of the compose files: no pull,
#                       no build, no database dump, and the rollback point stays as it is. This is
#                       how Redis is moved between the panel container and its own container
#                       (COMPOSE_PROFILES=external-redis, see .env.example): the redis container
#                       is created or removed as needed.
#   --no-backup         run: skip the database dump
#   --timeout SECONDS   run, rollback: how long to wait for healthy (default 300)
#   --dry-run           rollback: only show what it would do
#   --yes               restore-db: do it (without it the command only says what it would do)
#
# Files it writes, all in this deploy directory, mode 600 and ignored by git:
#   upgrade-state.env                   the rollback point (do not edit)
#   upgrade-backup-<time>.sql / .env    database dump and copy of .env (the newest 3 are kept)
#
# Needs bash, docker with the compose plugin, and git. Nothing else.
set -uo pipefail

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd) || exit 1
DEPLOY_DIR=$(dirname -- "$SCRIPT_DIR")
STATE_FILE="$DEPLOY_DIR/upgrade-state.env"
BACKUP_DIR="${MCPANEL_UPGRADE_BACKUP_DIR:-$DEPLOY_DIR}"
KEEP_BACKUPS=3
STAMP=$(date +%Y%m%d-%H%M%S)
STOP_SECONDS=120
REVISION_LABEL=io.mcpanel.revision
SERVER_LABEL=io.mcpanel.managed=true
RETIRE_SETTING=MCPANEL_RETIRE_STOPPED_CONTAINERS
SUPERVISE=/usr/local/bin/mcpanel-supervise.py

FORCE=0
WITH_MIGRATIONS=0
RESUME=0
FROM_OPTION=""
NO_PULL=0
NO_BUILD=0
NO_BACKUP=0
TIMEOUT=300
DRY=0
YES=0
RESTORE_FILE=""
TMP=""
PROJECT=""
WANT=""      # services of the source tree's compose file, space separated
FROM_REV=""  # the revision that is deployed now ("" = unknown / nothing deployed)
FROM_WHY=""
TO_REV=""
TO_LABEL=""
PREV_STATUS=""    # STATUS of the state file when this command began ("" = there is none)
CONTINUE=0        # run: go on with the rollback point of the run that did not finish
BLOCKED=""        # run: why it must not go on at all (a run or rollback that did not finish)
PLAN_MIGRATIONS=""  # run: migration files the source tree has and the deployed revision has not
MIGRATIONS_NEEDED=0 # run: the new version changes the database schema
MIGRATIONS_OK=0     # run: ... and that was allowed (--with-migrations, now or by the run this one continues)
PHASE=""          # run: "before-switch" from the pull to the switch, then "switching"
STEP=""           # the step that is running, for the note about an interruption
OWN_STATE=0       # this process wrote the state file: an interruption is noted in it
IMAGE_SNAPSHOT="" # lines "<image name>|<image id>" from before the pull and the build
SCRIPT_GONE=""    # rollback: said after every message once the checkout removed this script
UNCHANGED="nothing was changed (the old version is still running)"

say() { printf '%s\n' "$*"; }
step() {
    STEP=$*
    printf '\n==> %s\n' "$*"
}
warn() { printf 'upgrade: %s\n' "$*" >&2; }
die() {
    printf 'upgrade: %s\n' "$*" >&2
    [ -z "$SCRIPT_GONE" ] || printf 'upgrade: %s\n' "$SCRIPT_GONE" >&2
    exit 1
}
refuse() {
    printf 'upgrade: %s\n' "$*" >&2
    exit 2
}

usage() {
    say "usage: ./scripts/upgrade.sh {status|dry-run|run|rollback} [--force] [--with-migrations] [--resume] [--from REV]"
    say "                            [--no-pull] [--no-build] [--no-backup] [--timeout SECONDS] [--dry-run]"
    say "       ./scripts/upgrade.sh restore-db FILE [--yes]"
    say "The comment at the top of $0 explains every step."
}

dc() { (cd "$DEPLOY_DIR" && docker compose "$@"); }
g() { git -C "$DEPLOY_DIR" "$@"; }

in_list() { # in_list WORD LIST...
    local word="$1" item
    shift
    for item in "$@"; do
        [ "$item" = "$word" ] && return 0
    done
    return 1
}

short() { printf '%s' "${1:0:12}"; }

# --- leaving early ----------------------------------------------------------------------
# Runs at every exit. Before the switch a run has changed one thing only: the pull and the build
# moved image names to the new images. They are pointed back, so "nothing was changed" is true.
on_exit() {
    [ "$PHASE" = before-switch ] && restore_image_names
    [ -z "$TMP" ] || rm -rf "$TMP"
}

# A closed terminal session (SIGHUP), Ctrl-C or a kill: note it in the state file, so `status`
# and the next `run` say that the run did not finish and why.
on_signal() { # on_signal NAME NUMBER
    trap '' HUP INT TERM PIPE # the terminal may be gone: a message that cannot be written must not stop the rest
    local status
    if [ "$OWN_STATE" = 1 ] && status=$(state_get STATUS); then
        case "$status" in
            started | switching | rolling-back)
                printf 'INTERRUPTED=SIG%s at %s during: %s\n' "$1" "$(date '+%Y-%m-%d %H:%M:%S')" "${STEP:-the start}" >>"$STATE_FILE"
                ;;
        esac
    fi
    warn "interrupted by SIG$1 during: ${STEP:-the start}. What is left to do: ./scripts/upgrade.sh status"
    [ -z "$SCRIPT_GONE" ] || warn "$SCRIPT_GONE"
    exit $((128 + $2))
}

# --- what is there ----------------------------------------------------------------------
preflight() {
    command -v docker >/dev/null 2>&1 || die "docker is not installed"
    docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing (docker compose version)"
    command -v git >/dev/null 2>&1 || die "git is not installed"
    docker version --format '{{.Server.Version}}' >/dev/null 2>&1 || die "the Docker daemon does not answer"
    [ -f "$DEPLOY_DIR/docker-compose.yml" ] || die "$DEPLOY_DIR/docker-compose.yml not found"
    [ -f "$DEPLOY_DIR/.env" ] || die "$DEPLOY_DIR/.env not found (first install: ./scripts/generate-secrets.sh, see DEPLOYMENT.md)"
    g rev-parse --git-dir >/dev/null 2>&1 || die "$DEPLOY_DIR is not inside a git checkout (git says: $(g rev-parse --git-dir 2>&1 | head -n 1))"
    TMP=$(mktemp -d "${TMPDIR:-/tmp}/mcpanel-upgrade.XXXXXX") || die "cannot create a temporary directory"
    trap on_exit EXIT
    trap 'on_signal HUP 1' HUP
    trap 'on_signal INT 2' INT
    trap 'on_signal TERM 15' TERM
    local config line
    config=$(dc config 2>"$TMP/config.err") || die "the compose file or .env is not valid: $(head -n 3 "$TMP/config.err")"
    while IFS= read -r line; do
        case "$line" in
            "name: "*)
                PROJECT=${line#name: }
                PROJECT=${PROJECT//\"/}
                break
                ;;
        esac
    done <<<"$config"
    [ -n "$PROJECT" ] || die "cannot read the project name from 'docker compose config'"
    WANT=$(dc config --services 2>/dev/null | sort | tr '\n' ' ') || die "cannot list the services of the compose file"
    [ -n "${WANT// /}" ] || die "the compose file has no services"
}

# Lines "<id> <service> <state>" for every container of the project (not the one-off ones).
project_containers() {
    local id service state oneoff
    docker ps -a --filter "label=com.docker.compose.project=$PROJECT" \
        --format '{{.ID}} {{.Label "com.docker.compose.service"}} {{.State}} {{.Label "com.docker.compose.oneoff"}}' |
        while read -r id service state oneoff; do
            [ "$oneoff" = "True" ] || printf '%s %s %s\n' "$id" "$service" "$state"
        done | sort -k2
}

service_container() { # service_container SERVICE [running] -> id of its (running) container
    local id service state
    while read -r id service state; do
        if [ "$service" = "$1" ] && { [ "${2:-}" != running ] || [ "$state" = running ]; }; then
            printf '%s\n' "$id"
            return 0
        fi
    done < <(project_containers)
    return 1
}

stale_containers() { # containers of services the source tree's compose file does not have
    local id service state
    # shellcheck disable=SC2086 # WANT is a space separated list
    while read -r id service state; do
        in_list "$service" $WANT || printf '%s %s %s\n' "$id" "$service" "$state"
    done < <(project_containers)
}

psql_query() { # psql_query SQL -> rows ("|" between columns); fails if the database cannot be asked
    local pg
    pg=$(service_container postgres running) || return 1
    printf '%s\n' "$1" | docker exec -i "$pg" sh -c 'psql -X -q -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' 2>"$TMP/psql.err"
}

active_jobs() { # "<queued> <running>", or fails (database down, or no tables yet)
    local row
    row=$(psql_query "SELECT count(*) FILTER (WHERE status = 'queued'), count(*) FILTER (WHERE status = 'running') FROM jobs_job") || return 1
    printf '%s %s\n' "${row%%|*}" "${row##*|}"
}

migration_state() { # lines "<app> <last applied migration>"
    psql_query "SELECT app || ' ' || name FROM django_migrations m WHERE id = (SELECT max(id) FROM django_migrations WHERE app = m.app) ORDER BY app"
}

# Refuse (exit 2) while jobs are queued or running. $1 says when.
check_jobs() {
    local jobs queued running
    if ! service_container postgres running >/dev/null; then
        say "Jobs: the database is not running, nothing to check."
        return 0
    fi
    if ! jobs=$(active_jobs); then
        say "Jobs: could not ask the database ($(head -n 1 "$TMP/psql.err" 2>/dev/null)); assuming none."
        return 0
    fi
    read -r queued running <<<"$jobs"
    if [ "$queued" = 0 ] && [ "$running" = 0 ]; then
        say "Jobs: none queued or running ($1)."
        return 0
    fi
    say "Jobs: $queued queued, $running running ($1). Open the Activity page to see them."
    if [ "$FORCE" = 1 ]; then
        say "      --force: going on. Running jobs get $STOP_SECONDS s to finish and are marked failed after that."
        return 0
    fi
    [ "$DRY" = 1 ] && {
        say "      'run' would refuse now. Wait until they are done, or use --force."
        return 0
    }
    refuse "refusing while jobs are queued or running. Wait until the Activity page is empty, or use --force."
}

# Exited Minecraft server containers, which the new panel removes soon after it starts (dry-run
# and run say so once; the panel's default is not changed here).
notice_exited_servers() {
    local count=0 id line value="" has_line=0
    while read -r id; do
        [ -n "$id" ] && count=$((count + 1))
    done < <(docker ps -a --filter "label=$SERVER_LABEL" --filter status=exited --filter status=created --filter status=dead --format '{{.ID}}' 2>/dev/null)
    [ "$count" -gt 0 ] || return 0
    while IFS= read -r line; do
        case "$line" in
            "$RETIRE_SETTING="*)
                has_line=1
                value=${line#*=}
                ;;
        esac
    done <"$DEPLOY_DIR/.env"
    value=${value%$'\r'} # a .env edited on Windows
    value=${value//[\"\' ]/}
    if [ "$has_line" = 1 ] && [ -n "$value" ]; then
        case "$value" in
            0 | [Ff]alse | [Nn]o | [Oo]ff) say "Stopped servers: $count exited server container(s) on this host are kept ($RETIRE_SETTING=$value in .env)." ;;
        esac
        return 0
    fi
    say "Stopped servers: this host has $count exited Minecraft server container(s). A stopped server needs no"
    say "  container any more: shortly after the upgrade the panel removes these $count container(s). World data is"
    say "  not touched, the last 2,000 console lines of each are kept, and a stopped server is started from the"
    say "  panel as before (its container is created again). To look first, add this line to .env BEFORE the run:"
    say "      $RETIRE_SETTING=false"
    say "  They are kept then, and after the upgrade this lists what would be removed, without removing anything:"
    say "      docker compose exec panel python manage.py retire_stopped_containers --dry-run"
    say ""
}

# --- revisions --------------------------------------------------------------------------
services_of_revision() { # sorted service names of REV's compose file, with today's .env
    local rev="$1" file="$TMP/compose-$1.yml"
    g show "$rev:./docker-compose.yml" >"$file" 2>/dev/null || return 1
    (cd "$DEPLOY_DIR" && docker compose -f "$file" --project-directory "$DEPLOY_DIR" config --services 2>/dev/null) | sort | tr '\n' ' '
}

running_services() {
    local id service state seen=""
    while read -r id service state; do
        # shellcheck disable=SC2086
        in_list "$service" $seen || seen="$seen $service"
    done < <(project_containers)
    # shellcheck disable=SC2086
    printf '%s\n' $seen | sort | tr '\n' ' '
}

layout_matches() { # the compose file of REV has exactly the services that have containers now
    local theirs
    theirs=$(services_of_revision "$1") || return 1
    [ "$theirs" = "$(running_services)" ]
}

state_get() { # state_get KEY -> first value
    local key value
    [ -f "$STATE_FILE" ] || return 1
    while IFS='=' read -r key value; do
        if [ "$key" = "$1" ]; then
            printf '%s\n' "$value"
            return 0
        fi
    done <"$STATE_FILE"
    return 1
}

# Read it with <<<"$(state_all KEY)", never with < <(state_all KEY): a loop that ends early (break,
# return, die) would close the pipe under the writer, and where SIGPIPE is ignored (a systemd
# unit) every line left to write then prints "printf: write error: Broken pipe".
state_all() { # state_all KEY -> every value, one per line
    local key value
    [ -f "$STATE_FILE" ] || return 0
    while IFS='=' read -r key value; do
        [ "$key" = "$1" ] && printf '%s\n' "$value"
    done <"$STATE_FILE"
    return 0
}

unfinished() { # unfinished STATUS: a run or a rollback began and did not get to its end
    case "$1" in started | switching | rolling-back) return 0 ;; esac
    return 1
}

# Which git revision do the running containers come from? Sets FROM_REV and FROM_WHY.
find_deployed_revision() {
    local panel label image head orig
    FROM_REV=""
    FROM_WHY="nothing is deployed"
    [ -n "$(project_containers)" ] || return 0
    if [ -n "$FROM_OPTION" ]; then
        FROM_REV=$(g rev-parse --verify -q "$FROM_OPTION^{commit}") || die "--from $FROM_OPTION is not a revision of this checkout"
        FROM_WHY="given with --from"
        return 0
    fi
    if panel=$(service_container panel); then
        label=$(docker inspect -f "{{index .Config.Labels \"$REVISION_LABEL\"}}" "$panel" 2>/dev/null)
        label=${label%-dirty}
        if [[ "$label" =~ ^[0-9a-f]{7,40}$ ]] && FROM_REV=$(g rev-parse --verify -q "$label^{commit}"); then
            FROM_WHY="label of the running panel image"
            return 0
        fi
        image=$(docker inspect -f '{{.Image}}' "$panel" 2>/dev/null)
        if [ -n "$image" ] && [ "$(state_get DEPLOYED_PANEL_IMAGE)" = "$image" ] && FROM_REV=$(g rev-parse --verify -q "$(state_get DEPLOYED_REV)^{commit}"); then
            FROM_WHY="recorded by the last run of this script"
            return 0
        fi
    fi
    FROM_REV=""
    FROM_WHY="unknown"
    # This script deployed (or rolled back) the stack before: what it recorded and the image label
    # are the only evidence then. The layout is only guessed from on the very first run.
    [ -f "$STATE_FILE" ] && return 0
    # Images built before this script existed carry no label. Take the revision the checkout
    # was at before the last `git pull`, if its compose file fits the containers that exist.
    head=$(g rev-parse HEAD)
    if orig=$(g rev-parse --verify -q 'ORIG_HEAD^{commit}') && [ "$orig" != "$head" ] && g merge-base --is-ancestor "$orig" "$head" 2>/dev/null && layout_matches "$orig"; then
        FROM_REV=$orig
        FROM_WHY="assumed: where the checkout was before the last git pull; its compose file fits the running containers"
        return 0
    fi
    if layout_matches "$head"; then
        FROM_REV=$head
        FROM_WHY="assumed: the source tree's own revision; its compose file fits the running containers"
        return 0
    fi
    return 0
}

find_target_revision() {
    TO_REV=$(g rev-parse HEAD) || die "cannot read the revision of the source tree"
    TO_LABEL=$TO_REV
    if ! g diff --quiet HEAD -- 2>/dev/null; then
        TO_LABEL="$TO_REV-dirty"
    fi
}

describe_revision() { # describe_revision REV -> "abc1234 subject"
    g log -1 --format='%h %s' "$1" 2>/dev/null || printf '%s' "$(short "$1")"
}

new_migration_files() { # migration files the source tree has and the deployed revision has not
    [ -n "$FROM_REV" ] || return 0
    {
        g diff --name-only --diff-filter=A "$FROM_REV" HEAD -- ':(top)backend/apps' 2>/dev/null
        g status --porcelain --untracked-files=all -- ':(top)backend/apps' 2>/dev/null | while read -r flag path; do
            case "$flag" in '??' | A*) printf '%s\n' "$path" ;; esac
        done
    } | while IFS= read -r path; do
        case "$path" in */migrations/[0-9]*.py) printf '%s\n' "$path" ;; esac
    done | sort -u
}

# The `run` that does what was just shown or refused, with every option it needs.
next_command() {
    local cmd="./scripts/upgrade.sh run"
    [ "$RESUME" = 1 ] && cmd="$cmd --resume"
    [ "$NO_BUILD" = 1 ] && cmd="$cmd --no-build"
    if [ "$WITH_MIGRATIONS" = 1 ] || { [ "$MIGRATIONS_NEEDED" = 1 ] && [ "$MIGRATIONS_OK" != 1 ]; }; then
        cmd="$cmd --with-migrations"
    fi
    [ -n "$FROM_OPTION" ] && cmd="$cmd --from $FROM_OPTION"
    [ "$NO_PULL" = 1 ] && cmd="$cmd --no-pull"
    [ "$NO_BACKUP" = 1 ] && cmd="$cmd --no-backup"
    [ "$FORCE" = 1 ] && cmd="$cmd --force"
    [ "$TIMEOUT" = 300 ] || cmd="$cmd --timeout $TIMEOUT"
    printf '%s' "$cmd"
}

# --- a run that did not finish ------------------------------------------------------------
record_matches_containers() { # every container still has the image the state file recorded for its image name
    local id service state name image_id recorded rname rid tag any=0 images
    images=$(state_all IMAGE)
    while read -r id service state; do
        any=1
        name=$(docker inspect -f '{{.Config.Image}}' "$id" 2>/dev/null) || return 1
        image_id=$(docker inspect -f '{{.Image}}' "$id" 2>/dev/null) || return 1
        recorded=""
        while IFS='|' read -r rname rid tag; do
            [ "$rname" = "$name" ] && recorded=$rid
        done <<<"$images"
        [ "$recorded" = "$image_id" ] || return 1
    done < <(project_containers)
    [ "$any" = 1 ]
}

describe_unfinished() { # one line: which run or rollback did not finish
    local what="run" from to note
    [ "$PREV_STATUS" = rolling-back ] && what="rollback"
    from=$(state_get FROM_REV) || from=""
    to=$(state_get TO_REV) || to=""
    note=$(state_all INTERRUPTED | tail -n 1)
    printf "the last %s did not finish (state '%s', rollback point recorded %s, %s -> %s%s)" "$what" "$PREV_STATUS" \
        "$(state_get RECORDED || printf '?')" "$(short "$from")" "$(short "$to")" "${note:+; interrupted by $note}"
}

# Sets FROM_REV / FROM_WHY for `run`. A run that did not finish decides: its record is the origin
# and its rollback point stays (CONTINUE=1), or `run` must not go on at all (BLOCKED says why).
# Nothing is guessed from the layout then: half-switched containers fit neither version.
choose_origin() {
    local recorded given
    CONTINUE=0
    BLOCKED=""
    if ! unfinished "$PREV_STATUS"; then
        case "$PREV_STATUS" in
            "" | done | rolled-back) ;;
            *) die "unknown state '$PREV_STATUS' in $STATE_FILE" ;;
        esac
        [ "$RESUME" = 1 ] && die "there is no unfinished run to resume$([ -n "$PREV_STATUS" ] && printf " (the last one ended as '%s')" "$PREV_STATUS"): use 'run' without --resume"
        find_deployed_revision
        return 0
    fi
    [ "$RESUME" = 1 ] && [ "$NO_BUILD" = 1 ] && die "--resume and --no-build do not go together"
    recorded=$(state_get FROM_REV) || recorded=""
    FROM_REV=$(g rev-parse --verify -q "$recorded^{commit}") || die "$(describe_unfinished), and the revision it recorded is not in this checkout any more"
    FROM_WHY="recorded $(state_get RECORDED || printf '?') by the run that did not finish"
    if [ -n "$FROM_OPTION" ]; then
        given=$(g rev-parse --verify -q "$FROM_OPTION^{commit}") || die "--from $FROM_OPTION is not a revision of this checkout"
        [ "$given" = "$FROM_REV" ] || die "$(describe_unfinished): its rollback point says what was deployed, --from cannot change that"
    fi
    if [ "$RESUME" = 1 ]; then
        CONTINUE=1
    elif [ "$PREV_STATUS" = started ] && record_matches_containers; then
        CONTINUE=1 # that run switched nothing: the containers are the ones it recorded
    elif [ "$PREV_STATUS" = rolling-back ]; then
        BLOCKED="$(describe_unfinished): the stack may be half old, half new.
         Run 'rollback' again to finish it, or 'run --resume' to go to the version of the source tree again:
           ./scripts/upgrade.sh rollback       back to $(short "$FROM_REV") with the images saved then
           ./scripts/upgrade.sh run --resume   go on to the new version; the rollback point stays as recorded"
    else
        BLOCKED="$(describe_unfinished): the stack may be half old, half new.
         Run 'rollback' first, or 'run --resume' to continue it:
           ./scripts/upgrade.sh rollback       back to $(short "$FROM_REV") with the images saved then
           ./scripts/upgrade.sh run --resume   go on to the new version; the rollback point stays as recorded"
    fi
}

# --- showing things -----------------------------------------------------------------------
show_containers() {
    local id service state info mark name health image image_id
    printf '  %-16s %-26s %-9s %-10s %s\n' SERVICE CONTAINER STATE HEALTH IMAGE
    while read -r id service state; do
        info=$(docker inspect -f '{{.Name}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}|{{.Config.Image}}|{{.Image}}' "$id" 2>/dev/null) || continue
        IFS='|' read -r name health image image_id <<<"$info"
        mark=""
        # shellcheck disable=SC2086
        in_list "$service" $WANT || mark="   <- not in the source tree's layout: removed by 'run'"
        printf '  %-16s %-26s %-9s %-10s %s (%s)%s\n' "$service" "${name#/}" "$state" "$health" "$image" "$(short "${image_id#sha256:}")" "$mark"
    done < <(project_containers)
}

show_supervisor() { # the processes inside the panel container, if it is the single-container kind
    local panel
    panel=$(service_container panel running) || return 0
    # only the single panel container (command `all`) has a supervisor and its status file
    docker exec "$panel" test -f /run/mcpanel/supervisor.json 2>/dev/null || return 0
    say "Processes in the panel container:"
    docker exec "$panel" python "$SUPERVISE" status 2>&1 | while IFS= read -r line; do say "  $line"; done
    return "${PIPESTATUS[0]}"
}

show_rollback_point() {
    local status rev when entry name id tag
    if ! status=$(state_get STATUS); then
        say "Rollback point: none (no 'run' of this script yet)."
        return 0
    fi
    rev=$(state_get FROM_REV)
    when=$(state_get RECORDED)
    say "Rollback point: recorded $when, state '$status', revision $(short "$rev")"
    while IFS='|' read -r name id tag; do
        [ -n "$name" ] || continue
        if docker image inspect "$id" >/dev/null 2>&1; then
            say "  $name -> $(short "${id#sha256:}") (kept as $tag)"
        else
            say "  $name -> $(short "${id#sha256:}") IS GONE (rollback is not possible)"
        fi
    done <<<"$(state_all IMAGE)"
    entry=$(state_get BACKUP_SQL) && [ -n "$entry" ] && say "  database dump: $entry"
    if unfinished "$status"; then
        entry=$(state_all INTERRUPTED | tail -n 1)
        case "$status" in
            started) say "  The run that recorded it ended before the switch${entry:+ (interrupted by $entry)}: 'run' goes on with this rollback point." ;;
            switching) say "  The run that recorded it DID NOT FINISH${entry:+ (interrupted by $entry)}: 'rollback' goes back, 'run --resume' goes on." ;;
            rolling-back) say "  The rollback to it DID NOT FINISH${entry:+ (interrupted by $entry)}: 'rollback' again finishes it, 'run --resume' goes forward again." ;;
        esac
    fi
    return 0
}

cmd_status() {
    preflight
    find_deployed_revision
    find_target_revision
    local branch upstream remote_rev jobs
    say "MCPanel stack \"$PROJECT\" in $DEPLOY_DIR"
    say ""
    branch=$(g symbolic-ref -q --short HEAD) || branch="(detached)"
    say "Source tree:  $(describe_revision "$TO_REV")   [branch $branch$([ "$TO_LABEL" = "$TO_REV" ] || printf ', with local changes')]"
    if [ -n "$FROM_REV" ]; then
        say "Deployed:     $(describe_revision "$FROM_REV")   [$FROM_WHY]"
    else
        say "Deployed:     $FROM_WHY"
    fi
    if [ "$branch" != "(detached)" ] && upstream=$(g rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null); then
        remote_rev=$(run_limited 15 git -C "$DEPLOY_DIR" ls-remote "${upstream%%/*}" "refs/heads/${upstream#*/}" 2>/dev/null)
        remote_rev=${remote_rev%%[[:space:]]*}
        if [ -z "$remote_rev" ]; then
            say "Upstream:     $upstream could not be asked"
        elif [ "$remote_rev" = "$TO_REV" ]; then
            say "Upstream:     $upstream is at the same revision"
        elif g merge-base --is-ancestor "$remote_rev" HEAD 2>/dev/null; then
            say "Upstream:     $upstream is older than the source tree"
        else
            say "Upstream:     $upstream has other commits ($(short "$remote_rev")): 'git pull' gets them"
        fi
    fi
    say "Services the source tree wants: $WANT"
    say ""
    if [ -z "$(project_containers)" ]; then
        say "Containers: none."
    else
        say "Containers:"
        show_containers
    fi
    say ""
    show_supervisor
    if jobs=$(active_jobs 2>/dev/null); then
        say "Jobs: ${jobs% *} queued, ${jobs#* } running"
    else
        say "Jobs: unknown (the database is not running or has no tables yet)"
    fi
    show_rollback_point
}

run_limited() { # run_limited SECONDS COMMAND...: with coreutils timeout if there is one
    local seconds="$1"
    shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$seconds" "$@"
    else
        "$@"
    fi
}

# --- run ----------------------------------------------------------------------------------------
show_plan() {
    local id service state
    say "MCPanel stack \"$PROJECT\" in $DEPLOY_DIR"
    if [ -n "$FROM_REV" ]; then
        say "  from:  $(describe_revision "$FROM_REV")   [$FROM_WHY]"
        case "$FROM_WHY" in assumed*) say "         (an assumption: if another revision is deployed, pass it with --from <revision>)" ;; esac
    else
        say "  from:  $FROM_WHY"
    fi
    say "  to:    $(describe_revision "$TO_REV")$([ "$TO_LABEL" = "$TO_REV" ] || printf '   [with local changes]')"
    if [ -n "$FROM_REV" ] && [ "$FROM_REV" != "$TO_REV" ]; then
        say "  commits: $(g rev-list --count "$FROM_REV..$TO_REV" 2>/dev/null || printf '?') new, $(g rev-list --count "$TO_REV..$FROM_REV" 2>/dev/null || printf '?') dropped"
    elif [ -n "$FROM_REV" ] && [ "$NO_BUILD" = 1 ]; then
        say "  same revision"
    elif [ -n "$FROM_REV" ] && [ "$NO_PULL" = 1 ]; then
        say "  same revision: images are rebuilt (with the base images on this host)"
    elif [ -n "$FROM_REV" ]; then
        say "  same revision: images are rebuilt on fresh base images"
    fi
    if [ "$CONTINUE" = 1 ]; then
        say "  $(describe_unfinished):"
        say "  this run goes on with that rollback point and makes no new one"
    fi
    if [ "$NO_BUILD" = 1 ]; then
        say "  --no-build: the images stay as they are; only .env / compose changes are applied"
    fi
    say "  services afterwards: $WANT"
    if [ -n "$(stale_containers)" ]; then
        say "  containers that will be stopped (up to $STOP_SECONDS s) and removed:"
        while read -r id service state; do
            say "    $service ($(docker inspect -f '{{.Name}}' "$id" 2>/dev/null | cut -c2-), $state)"
        done < <(stale_containers)
    fi
    if [ -n "$PLAN_MIGRATIONS" ]; then
        if [ "$WITH_MIGRATIONS" = 1 ]; then
            say "  database migrations in this version (--with-migrations):"
        elif [ "$MIGRATIONS_OK" = 1 ]; then
            say "  database migrations in this version (allowed by the run that did not finish):"
        else
            say "  database migrations in this version (run needs --with-migrations):"
        fi
        while IFS= read -r id; do say "    $id"; done <<<"$PLAN_MIGRATIONS"
    fi
}

require_known_origin() {
    local panel
    [ -z "$(project_containers)" ] && return 0
    [ -n "$FROM_REV" ] && return 0
    panel=$(service_container panel) || panel="<panel container>"
    die "cannot tell which git revision the running containers were built from: the panel image has no
         revision label, no earlier run of this script recorded it, and the revision the checkout was at before
         the last 'git pull' (ORIG_HEAD; a second pull overwrites it) does not have exactly the services that
         are running ($(running_services)). Nothing was changed. Pass the deployed revision:
           ./scripts/upgrade.sh run --from <revision>
         How to find it:
           the label of the running panel image, if it has one:
             docker inspect -f '{{index .Config.Labels \"$REVISION_LABEL\"}}' $panel
           otherwise the commit the checkout was at before you pulled: 'git reflog -n 20' lists the pulls
           (\"pull: Fast-forward\"); the entry below the first pull since the last deployment is that commit."
}

state_edit() { # state_edit "KEY..." [LINE...]: drop every line of these keys, then add the lines
    local tmp="$STATE_FILE.tmp.$$" key line patterns=()
    for key in $1; do patterns+=(-e "^$key="); done
    shift
    (
        umask 077
        {
            grep -v "${patterns[@]}" "$STATE_FILE"
            for line in "$@"; do printf '%s\n' "$line"; done
            :
        } >"$tmp"
    ) || die "cannot write $tmp"
    mv -f "$tmp" "$STATE_FILE" || die "cannot write $STATE_FILE"
}

# Only for a run that starts from a finished state (none, done, rolled-back): a run that did not
# finish keeps its rollback point (choose_origin), so the tags of the images it saved can never
# end up among the obsolete ones.
record_rollback_point() { # record_rollback_point BRANCH
    local id service state name image_id tag repo seen="" lines="" obsolete="" entry old
    unfinished "$PREV_STATUS" && die "internal error: a new rollback point on top of a run that did not finish"
    # tags of older rollback points go away once this run has succeeded
    while IFS='|' read -r name image_id tag; do
        [ -n "$tag" ] && obsolete="$obsolete $tag"
    done <<<"$(state_all IMAGE)"
    for old in $(state_all OBSOLETE_TAG); do obsolete="$obsolete $old"; done

    while read -r id service state; do
        name=$(docker inspect -f '{{.Config.Image}}' "$id") || die "cannot inspect container $id"
        image_id=$(docker inspect -f '{{.Image}}' "$id") || die "cannot inspect container $id"
        # shellcheck disable=SC2086
        in_list "$name" $seen && continue
        seen="$seen $name"
        docker image inspect "$image_id" >/dev/null 2>&1 || die "the image of $service ($image_id) is gone; cannot record a rollback point"
        repo=${name%%@*}
        repo=${repo%:*}
        tag="mcpanel-rollback/${repo//\//-}:$STAMP"
        if docker image inspect "$tag" >/dev/null 2>&1; then
            die "the rollback tag $tag exists already; not overwriting it (try again in a second)"
        fi
        docker image tag "$image_id" "$tag" || die "cannot tag $image_id as $tag"
        say "  $name = $(short "${image_id#sha256:}"), kept as $tag"
        lines="${lines}IMAGE=$name|$image_id|$tag"$'\n'
    done < <(project_containers)

    if entry=$(migration_state); then
        while read -r name tag; do
            [ -n "$name" ] && lines="${lines}MIGRATION=$name $tag"$'\n'
        done <<<"$entry"
        lines="${lines}MIGRATIONS_RECORDED=yes"$'\n'
        say "  database migrations: $(grep -c . <<<"$entry") apps recorded"
    else
        lines="${lines}MIGRATIONS_RECORDED=no"$'\n'
        say "  database migrations: not recorded (the database is not running)"
    fi
    for old in $obsolete; do lines="${lines}OBSOLETE_TAG=$old"$'\n'; done
    lines="${lines}RECORDED=$(date '+%Y-%m-%d %H:%M:%S')"$'\n'"FROM_REV=$FROM_REV"$'\n'"TO_REV=$TO_REV"$'\n'"BRANCH=$1"$'\n'
    if [ "$MIGRATIONS_OK" = 1 ]; then
        lines="${lines}ALLOWED_MIGRATIONS=yes"$'\n'
    fi
    OWN_STATE=1
    write_state "STATUS=started" "$lines"
}

# The rollback point of the run that did not finish stays as it is; only where this run goes is new.
keep_rollback_point() { # keep_rollback_point BRANCH
    local name id tag gone=0
    say "  recorded $(state_get RECORDED || printf '?'): revision $(short "$FROM_REV")"
    while IFS='|' read -r name id tag; do
        [ -n "$name" ] || continue
        if docker image inspect "$id" >/dev/null 2>&1; then
            say "  $name = $(short "${id#sha256:}"), kept as $tag"
        else
            say "  $name = $(short "${id#sha256:}") IS GONE"
            gone=1
        fi
    done <<<"$(state_all IMAGE)"
    [ "$gone" = 0 ] || warn "an image of the rollback point is gone (see above): 'rollback' will not be possible"
    OWN_STATE=1
    state_edit "TO_REV BRANCH INTERRUPTED" "TO_REV=$TO_REV" "BRANCH=$1"
    if [ "$MIGRATIONS_OK" = 1 ]; then
        state_edit "ALLOWED_MIGRATIONS" "ALLOWED_MIGRATIONS=yes"
    fi
}

schema_as_recorded() { # the database has exactly the migrations the rollback point recorded
    local now
    [ "$(state_get MIGRATIONS_RECORDED)" = yes ] || return 1
    now=$(migration_state) || return 1
    [ "$now" = "$(state_all MIGRATION)" ]
}

write_state() { # write_state STATUS=... [MORE LINES]
    local tmp="$STATE_FILE.tmp.$$" body="${2:-}"
    if [ -z "$body" ] && [ -f "$STATE_FILE" ]; then
        body=$(grep -v -e '^#' -e '^STATUS=' -e '^DEPLOYED_' -e '^INTERRUPTED=' "$STATE_FILE")$'\n'
    fi
    (
        umask 077
        {
            printf '# Rollback point of deploy/scripts/upgrade.sh. Do not edit.\n'
            printf '%s\n' "$1"
            printf '%s' "$body"
            shift
            [ $# -gt 0 ] && shift
            for extra in "$@"; do printf '%s\n' "$extra"; done
        } >"$tmp"
    ) || die "cannot write $tmp"
    mv -f "$tmp" "$STATE_FILE" || die "cannot write $STATE_FILE"
}

backup_database() {
    local pg sql="$BACKUP_DIR/upgrade-backup-$STAMP.sql" env="$BACKUP_DIR/upgrade-backup-$STAMP.env" files index
    if [ "$NO_BACKUP" = 1 ]; then
        say "  skipped (--no-backup)"
        return 0
    fi
    if ! pg=$(service_container postgres running); then
        say "  the database is not running: nothing to dump"
        return 0
    fi
    (
        umask 077
        docker exec "$pg" sh -c 'pg_dump -U "$POSTGRES_USER" --exclude-table-data=django_session "$POSTGRES_DB"' >"$sql.part" &&
            cp "$DEPLOY_DIR/.env" "$env"
    ) || {
        rm -f "$sql.part"
        die "the database dump failed; $UNCHANGED"
    }
    mv -f "$sql.part" "$sql"
    chmod 600 "$sql" "$env"
    say "  $sql ($(wc -c <"$sql" | tr -d ' ') bytes, without login sessions)"
    say "  $env (copy of .env: its secret key decrypts what the dump holds; keep both private)"
    state_edit "BACKUP_SQL BACKUP_ENV" "BACKUP_SQL=$sql" "BACKUP_ENV=$env"
    # keep the newest few (the names sort by time)
    files=("$BACKUP_DIR"/upgrade-backup-[0-9]*-[0-9]*.sql)
    for ((index = 0; index < ${#files[@]} - KEEP_BACKUPS; index++)); do
        [ -f "${files[index]}" ] || continue
        rm -f "${files[index]}" "${files[index]%.sql}.env"
        say "  removed the old backup ${files[index]##*/}"
    done
}

# The pull and the build move image names (mcpanel/panel:local, postgres:16-alpine) to the new
# images. Until the switch they are pointed back at every exit (on_exit).
snapshot_image_names() {
    local name id
    IMAGE_SNAPSHOT=""
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        case "$name" in *@*) continue ;; esac # pinned by digest: it never moves
        id=$(docker image inspect -f '{{.Id}}' "$name" 2>/dev/null) || continue
        IMAGE_SNAPSHOT="${IMAGE_SNAPSHOT}${name}|${id}"$'\n'
    done < <(dc config --images 2>/dev/null | sort -u)
}

restore_image_names() {
    local name id now
    [ -n "$IMAGE_SNAPSHOT" ] || return 0
    while IFS='|' read -r name id; do
        [ -n "$name" ] || continue
        now=$(docker image inspect -f '{{.Id}}' "$name" 2>/dev/null) || now=""
        [ "$now" = "$id" ] && continue
        if docker image tag "$id" "$name" 2>/dev/null; then
            warn "$name points at the image it had before this run again ($(short "${id#sha256:}"))"
        else
            warn "$name still points at the image of the new version. To point it back: docker image tag $id $name"
        fi
    done <<<"$IMAGE_SNAPSHOT"
    IMAGE_SNAPSHOT=""
}

check_new_migrations() {
    local plan
    if ! service_container postgres running >/dev/null; then
        say "  the database is not running: migrations are applied at the first start"
        return 0
    fi
    if dc run --rm --no-deps -T panel manage migrate --check </dev/null >"$TMP/migrate.out" 2>&1; then
        say "  none: the database schema stays as it is"
        return 0
    fi
    plan=$(dc run --rm --no-deps -T panel manage showmigrations --plan </dev/null 2>/dev/null | grep -F '[ ]')
    if [ -z "$plan" ]; then
        tail -n 5 "$TMP/migrate.out"
        die "could not ask the new version for its database migrations; $UNCHANGED"
    fi
    say "  the new version changes the database schema:"
    while IFS= read -r line; do say "   $line"; done <<<"$plan"
    MIGRATIONS_NEEDED=1
    if [ "$MIGRATIONS_OK" != 1 ]; then
        # only reached when the plan did not show them (it compares the migration files of the two revisions)
        refuse "refusing without --with-migrations; $UNCHANGED.
         A rollback will undo these migrations again (data written into new columns is lost then). To do it:
           $(next_command)"
    fi
    if [ "$WITH_MIGRATIONS" = 1 ]; then
        say "  --with-migrations: they are applied when the new panel starts; 'rollback' undoes them"
    else
        say "  allowed by the run that did not finish (--with-migrations): applied when the new panel starts"
    fi
    if [ -f "$STATE_FILE" ]; then
        state_edit "MIGRATED" "MIGRATED=yes"
    fi
}

remove_stale_containers() {
    local id service state ids="" names=""
    while read -r id service state; do
        ids="$ids $id"
        names="$names $service"
    done < <(stale_containers)
    [ -n "$ids" ] || return 0
    say "  stopping$names (up to $STOP_SECONDS s each, all at once)"
    for id in $ids; do
        docker stop -t "$STOP_SECONDS" "$id" >/dev/null &
    done
    wait
    # shellcheck disable=SC2086
    docker rm $ids >/dev/null || die "could not remove the old containers:$names"
    say "  removed$names"
}

wait_healthy() {
    local deadline=$((SECONDS + TIMEOUT)) pending service id state last=""
    while :; do
        pending=""
        for service in $WANT; do
            if ! id=$(service_container "$service"); then
                pending="$pending $service(no container)"
                continue
            fi
            state=$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$id" 2>/dev/null)
            case "$state" in
                running/healthy | running/no-healthcheck) ;;
                *) pending="$pending $service($state)" ;;
            esac
        done
        if [ -z "$pending" ]; then
            say "  all healthy after $((SECONDS - (deadline - TIMEOUT))) s: $WANT"
            return 0
        fi
        if [ "$pending" != "$last" ]; then
            say "  waiting for:$pending"
            last=$pending
        fi
        if [ "$SECONDS" -ge "$deadline" ]; then
            say "  NOT healthy after $TIMEOUT s:$pending"
            for service in $WANT; do
                id=$(service_container "$service") || continue
                state=$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}ok{{end}}' "$id" 2>/dev/null)
                case "$state" in running/healthy | running/ok) continue ;; esac
                say "  --- $service: last health check"
                docker inspect -f '{{if .State.Health}}{{range .State.Health.Log}}{{.Output}}{{end}}{{end}}' "$id" 2>/dev/null | tail -n 3
                say "  --- $service: last log lines"
                docker logs --tail 25 "$id" 2>&1 | cut -c1-300
            done
            return 1
        fi
        sleep 3
    done
}

verify_layout() {
    local problems=0 id service state name schedulers
    if [ -n "$(stale_containers)" ]; then
        while read -r id service state; do
            say "  STILL THERE: $service ($state)"
        done < <(stale_containers)
        problems=1
    fi
    for name in worker worker-periodic beat redis; do
        # shellcheck disable=SC2086
        if in_list "$name" $WANT; then
            say "  $name: part of this layout"
        elif service_container "$name" >/dev/null; then
            problems=1
        else
            say "  $name: no container left"
        fi
    done
    if ! show_supervisor; then
        say "  the panel container reports a process that is not running"
        problems=1
    fi
    schedulers=$(count_schedulers)
    if [ "$schedulers" = 1 ]; then
        say "  schedulers (celery beat) running in the project: 1"
    else
        say "  schedulers (celery beat) running in the project: $schedulers, expected exactly 1"
        problems=1
    fi
    return "$problems"
}

count_schedulers() { # celery beat processes in all running containers of the project
    local id service state total=0 count
    while read -r id service state; do
        [ "$state" = running ] || continue
        # [b]eat: the pattern must not match the command line of this very search
        # shellcheck disable=SC2016 # the script runs inside the container
        count=$(docker exec "$id" sh -c 'n=0; for f in /proc/[0-9]*/cmdline; do if tr "\0" " " <"$f" 2>/dev/null | grep -q "celery -A mcpanel [b]eat"; then n=$((n+1)); fi; done; echo "$n"' </dev/null 2>/dev/null) || count=0
        case "$count" in '' | *[!0-9]*) count=0 ;; esac
        total=$((total + count))
    done < <(project_containers)
    printf '%s\n' "$total"
}

show_changes() { # show_changes: old and new image per image name, from the state file
    local name old tag now
    while IFS='|' read -r name old tag; do
        [ -n "$name" ] || continue
        now=$(docker image inspect -f '{{.Id}}' "$name" 2>/dev/null) || now="(gone)"
        if [ "$now" = "$old" ]; then
            say "  $name: unchanged ($(short "${old#sha256:}"))"
        else
            say "  $name: $(short "${old#sha256:}") -> $(short "${now#sha256:}")"
        fi
    done <<<"$(state_all IMAGE)"
}

cmd_run() {
    preflight
    PREV_STATUS=$(state_get STATUS) || PREV_STATUS=""
    find_target_revision
    choose_origin
    [ "$NO_BUILD" = 1 ] || PLAN_MIGRATIONS=$(new_migration_files)
    [ -z "$PLAN_MIGRATIONS" ] || MIGRATIONS_NEEDED=1
    if [ "$WITH_MIGRATIONS" = 1 ] || { [ "$CONTINUE" = 1 ] && [ "$(state_get ALLOWED_MIGRATIONS)" = yes ]; }; then
        MIGRATIONS_OK=1
    fi
    show_plan
    if [ -n "$BLOCKED" ]; then
        [ "$DRY" = 1 ] || refuse "$BLOCKED"
    elif [ "$NO_BUILD" != 1 ] && [ "$CONTINUE" != 1 ]; then
        require_known_origin
    fi
    # Decided here, before anything is recorded, dumped, pulled or built: the plan knows them.
    if [ -n "$PLAN_MIGRATIONS" ] && [ "$MIGRATIONS_OK" != 1 ] && [ "$DRY" != 1 ]; then
        refuse "this version changes the database schema (the migrations listed above), which 'run' only does with
         --with-migrations. Nothing was changed. A rollback undoes these migrations again (data written into
         new columns is lost then). To do it:
           $(next_command)"
    fi
    say ""
    [ "$NO_BUILD" = 1 ] || notice_exited_servers
    check_jobs "before the switch"

    if [ "$DRY" = 1 ]; then
        step "docker compose --dry-run up -d --remove-orphans (nothing is changed)"
        local line previous="" id service state name gone="" skip
        # Compose only stops what it calls orphans. This script removes every container of a service
        # the layout no longer has itself, before Compose runs (redis is one Compose would keep).
        while read -r id service state; do
            name=$(docker inspect -f '{{.Name}}' "$id" 2>/dev/null) || continue
            gone="$gone ${name#/}"
        done < <(stale_containers)
        [ -z "$gone" ] || say "  stopped and removed by this script before that:$gone"
        while IFS= read -r line; do # compose prints every event twice
            case "$line" in "$previous" | *" Running "* | *" Running" | "") continue ;; esac
            skip=0
            for name in $gone; do
                case "$line" in *"Container $name "*) skip=1 ;; esac
            done
            [ "$skip" = 1 ] && continue
            say "  $line"
            previous=$line
        done < <(dc --dry-run up -d --remove-orphans 2>&1)
        say ""
        show_rollback_point
        say ""
        if [ -n "$BLOCKED" ]; then
            say "Nothing was changed. 'run' would refuse now:"
            say "  $BLOCKED"
        else
            say "Nothing was changed. To do it: $(next_command)"
        fi
        return 0
    fi

    local had_containers=0 point=0 branch panel_image old
    [ -n "$(project_containers)" ] && had_containers=1
    branch=$(g symbolic-ref -q --short HEAD) || branch=""

    if [ "$NO_BUILD" = 1 ]; then
        apply_configuration
        return 0
    fi

    if [ "$CONTINUE" = 1 ]; then
        point=1
        step "Rollback point: the one of the run that did not finish (no new one is made)"
        keep_rollback_point "$branch"
        step "Backing up the database"
        if schema_as_recorded; then
            backup_database
        else
            # a dump taken now could hold the schema of the new version: it would be no way back
            say "  the database cannot be asked, or no longer has the schema the rollback point recorded: no new dump"
            say "  the dump from before the upgrade stays: $(state_get BACKUP_SQL || printf 'none was taken')"
        fi
        if [ "$PREV_STATUS" != started ]; then
            UNCHANGED="this attempt changed nothing (the stack is as the run that did not finish left it: 'rollback' or 'run --resume')"
        fi
    elif [ "$had_containers" = 1 ]; then
        point=1
        step "Recording the rollback point"
        record_rollback_point "$branch"
        step "Backing up the database"
        backup_database
    else
        step "Recording the rollback point"
        say "  nothing is running: there is nothing to go back to"
    fi

    export MCPANEL_REVISION="$TO_LABEL"
    snapshot_image_names
    PHASE=before-switch
    if [ "$NO_PULL" = 1 ]; then
        step "Building panel and agent (with the base images on this host)"
        dc build || die "the build failed; $UNCHANGED"
    else
        step "Pulling upstream images"
        dc pull --ignore-buildable || die "pulling failed; $UNCHANGED. Without internet: --no-pull"
        step "Building panel and agent on fresh base images (docker compose build --pull)"
        dc build --pull || die "the build failed; $UNCHANGED"
    fi

    step "Database migrations of the new version"
    check_new_migrations

    step "Switching over"
    check_jobs "right before the switch"
    PHASE=switching
    [ "$point" = 1 ] && write_state "STATUS=switching"
    remove_stale_containers
    dc up -d --remove-orphans || {
        warn "docker compose up failed. See the messages above."
        [ "$point" = 1 ] && warn "The run did not finish. To go back: ./scripts/upgrade.sh rollback   To try again: ./scripts/upgrade.sh run --resume"
        exit 1
    }

    step "Waiting for the services (up to $TIMEOUT s)"
    if ! wait_healthy; then
        warn "the new version is running but not healthy."
        [ "$point" = 1 ] && warn "Look at 'docker compose logs --tail 100 panel'. To go back: ./scripts/upgrade.sh rollback   To try again: ./scripts/upgrade.sh run --resume"
        exit 1
    fi

    step "Checking the layout"
    if ! verify_layout; then
        warn "the layout is not as expected (see above)."
        [ "$point" = 1 ] && warn "To go back: ./scripts/upgrade.sh rollback   To try again: ./scripts/upgrade.sh run --resume"
        exit 1
    fi

    step "Done"
    if [ "$point" = 1 ]; then
        panel_image=$(docker inspect -f '{{.Image}}' "$(service_container panel)" 2>/dev/null)
        write_state "STATUS=done" "" "DEPLOYED_REV=$TO_REV" "DEPLOYED_PANEL_IMAGE=$panel_image"
        PHASE=""
        if [ "$FROM_REV" != "$TO_REV" ]; then
            say "  revision: $(describe_revision "$FROM_REV")"
            say "        ->  $(describe_revision "$TO_REV")"
        else
            say "  revision: unchanged ($(short "$TO_REV")), images rebuilt"
        fi
        show_changes
        # tags of the rollback point before this one (recorded when this run began from a finished state)
        for old in $(state_all OBSOLETE_TAG); do
            docker image rm "$old" >/dev/null 2>&1 && say "  dropped the older rollback tag $old"
        done
        state_edit "OBSOLETE_TAG"
        say "  to go back: ./scripts/upgrade.sh rollback"
    else
        PHASE=""
        say "  the stack is up: $WANT"
    fi
    # shellcheck disable=SC2086
    if ! in_list redis $WANT && docker volume inspect "${PROJECT}_redis_data" >/dev/null 2>&1; then
        say "  The volume ${PROJECT}_redis_data of the earlier Redis container still exists (going back to"
        say "  that layout uses it). When you are sure you stay on this one: docker volume rm ${PROJECT}_redis_data"
    fi
    say ""
    show_containers
}

# `run --no-build`: bring the containers in line with .env and the compose files, nothing else.
apply_configuration() {
    local image
    while IFS= read -r image; do # the locally built images must be there already: this mode never builds
        case "$image" in
            mcpanel/*) docker image inspect "$image" >/dev/null 2>&1 || die "the image $image does not exist yet: run without --no-build first" ;;
        esac
    done < <(dc config --images 2>/dev/null)
    step "Applying the configuration (no build, the rollback point is not touched)"
    check_jobs "right before the switch"
    remove_stale_containers
    dc up -d --remove-orphans --no-build || die "docker compose up failed (see above)"
    step "Waiting for the services (up to $TIMEOUT s)"
    wait_healthy || die "not healthy; see 'docker compose logs --tail 100 panel'"
    step "Checking the layout"
    verify_layout || die "the layout is not as expected (see above)"
    step "Done"
    show_containers
    say ""
    show_rollback_point
}

# --- rollback -------------------------------------------------------------------------------------
again_flags() { # what `run` needs to do the recorded upgrade once more: " --with-migrations" or nothing
    if [ "$(state_get ALLOWED_MIGRATIONS)" = yes ] || [ "$(state_get MIGRATED)" = yes ]; then
        printf ' --with-migrations'
    fi
}

migrations_to_undo() { # lines "<app> <target>" (target "zero" for apps that had no migration)
    local now app name recorded found before
    now=$(migration_state) || return 1
    before=$(state_all MIGRATION)
    while read -r app name; do
        [ -n "$app" ] || continue
        found=""
        while read -r recorded; do
            if [ "${recorded%% *}" = "$app" ]; then
                found=${recorded#* }
                break
            fi
        done <<<"$before"
        if [ -z "$found" ]; then
            printf '%s zero\n' "$app"
        elif [ "$found" != "$name" ]; then
            printf '%s %s\n' "$app" "$found"
        fi
    done <<<"$now"
}

other_services() { # services of the project that have a running container, without the database
    local id service state seen=""
    while read -r id service state; do
        [ "$state" = running ] && [ "$service" != postgres ] && seen="$seen $service"
    done < <(project_containers)
    printf '%s' "${seen# }"
}

dump_way_back() { # how to get the database of before the upgrade back, as lines for a message
    local sql others
    sql=$(state_get BACKUP_SQL) || sql=""
    if [ -z "$sql" ]; then
        printf '%s\n' "         No database dump was taken by that run (--no-backup, or the database was down): the only" \
            "         other way back is a backup of the database of your own."
        return 0
    fi
    others=$(other_services)
    printf '%s\n' "         The way back is the database dump from before the upgrade. Everything the panel wrote" \
        "         since then (settings, servers created, job history) is lost; server files are not touched:"
    [ -z "$others" ] || printf '           docker compose stop %s\n' "$others"
    printf '%s\n' "           ./scripts/upgrade.sh restore-db $sql --yes" \
        "           ./scripts/upgrade.sh rollback"
}

# Ask the upgraded version whether every migration can be undone: before anything is stopped, and
# once more when the list changed while the containers were stopping.
check_undo_plan() { # check_undo_plan LINES ("<app> <target>") WHAT-IS-TRUE-NOW
    local name tag bad
    while read -r name tag; do
        [ -n "$name" ] || continue
        # </dev/null: `docker compose run` reads its standard input to the end, here the rest of this list
        if ! dc run --rm --no-deps -T panel manage migrate --plan "$name" "$tag" </dev/null >"$TMP/plan.out" 2>&1; then
            tail -n 10 "$TMP/plan.out"
            die "could not ask the upgraded version how to undo the migrations of '$name' (see above). $2"
        fi
        if bad=$(grep -m 1 -F IRREVERSIBLE "$TMP/plan.out"); then
            refuse "the database migrations of '$name' cannot be undone (${bad#"${bad%%[![:space:]]*}"}). $2
$(dump_way_back)"
        fi
    done <<<"$1"
    say "  asked the upgraded version: every one of them can be undone"
}

cmd_rollback() {
    preflight
    local status from to branch name id tag undo="" seen="" db_ok=0 head service state ids="" names="" line top stopped=""
    status=$(state_get STATUS) || die "there is no rollback point ($STATE_FILE does not exist)"
    PREV_STATUS=$status
    case "$status" in
        switching | done | rolling-back) ;;
        started)
            # the containers were changed since (not by this script): going back to the record is right
            record_matches_containers && die "the last run stopped before it changed anything: the old version is still running, there is nothing to roll back (to try the upgrade again: ./scripts/upgrade.sh run$(again_flags))"
            ;;
        rolled-back) die "the last upgrade was rolled back already (to upgrade again: git checkout $(state_get BRANCH || printf '<branch>'), then ./scripts/upgrade.sh run$(again_flags))" ;;
        *) die "unknown state '$status' in $STATE_FILE" ;;
    esac
    from=$(state_get FROM_REV)
    to=$(state_get TO_REV)
    branch=$(state_get BRANCH) || branch=""
    [ -n "$from" ] || die "the rollback point has no revision"
    g cat-file -e "$from^{commit}" 2>/dev/null || die "revision $from is not in this checkout any more"
    head=$(g rev-parse HEAD)
    top=$(g rev-parse --show-toplevel 2>/dev/null) || top="<mcpanel source>"

    say "MCPanel stack \"$PROJECT\" in $DEPLOY_DIR"
    say "  now:      $(describe_revision "$head")"
    say "  back to:  $(describe_revision "$from")   [recorded $(state_get RECORDED)]"
    while IFS='|' read -r name id tag; do
        [ -n "$name" ] || continue
        docker image inspect "$id" >/dev/null 2>&1 || die "the saved image of $name ($id) is gone: this rollback point cannot be used"
        say "  image:    $name -> $(short "${id#sha256:}")"
    done <<<"$(state_all IMAGE)"
    if [ "$from" != "$head" ] && ! g diff --quiet HEAD -- 2>/dev/null; then
        die "the source tree has local changes; git cannot switch to $(short "$from") safely. Commit or stash them first."
    fi
    if [ "$(state_get MIGRATIONS_RECORDED)" = yes ]; then
        if undo=$(migrations_to_undo); then
            if [ -n "$undo" ]; then
                say "  database migrations to undo:"
                while read -r line; do say "    $line"; done <<<"$undo"
                check_undo_plan "$undo" "Nothing was changed."
            fi
            seen=$undo
            db_ok=1
        else
            warn "the database does not answer: migrations of the upgrade cannot be undone"
            [ "$FORCE" = 1 ] || die "start the database first (docker compose up -d postgres), or use --force to go back without that"
        fi
    fi
    say ""
    check_jobs "before the rollback"
    if [ "$DRY" = 1 ]; then
        say ""
        say "Nothing was changed. To do it: ./scripts/upgrade.sh rollback"
        return 0
    fi

    # From here on things change. A rollback that does not get to its end is recorded as such:
    # `run` then refuses to make a new rollback point from a half-rolled-back stack.
    OWN_STATE=1
    STEP="the rollback"
    write_state "STATUS=rolling-back"

    if [ "$db_ok" = 1 ]; then
        step "Stopping everything but the database (up to $STOP_SECONDS s)"
        # nothing may use the new schema while it is taken away
        while read -r id service state; do
            [ "$service" = postgres ] && continue
            ids="$ids $id"
            names="$names $service"
        done < <(project_containers)
        for id in $ids; do docker stop -t "$STOP_SECONDS" "$id" >/dev/null & done
        wait
        say "  stopped:${names:- nothing was running}"
        # Only now the list is final: a panel that was still starting may have applied more
        # migrations after the list above was made.
        stopped="The containers are stopped, not removed, and the database was not changed. To go forward again: ./scripts/upgrade.sh run --resume --no-pull"
        undo=$(migrations_to_undo) || die "the database does not answer any more. $stopped"
        if [ "$undo" != "$seen" ] && [ -n "$undo" ]; then
            say "  the database changed while the containers were stopping. Migrations to undo now:"
            while read -r line; do say "    $line"; done <<<"$undo"
            check_undo_plan "$undo" "$stopped"
        fi
    fi
    if [ -n "$undo" ]; then
        step "Undoing the database migrations of the upgrade"
        while read -r name tag; do
            say "  $name -> $tag"
            # </dev/null: `docker compose run` reads its standard input to the end, here the rest of this list
            dc run --rm --no-deps -T panel manage migrate "$name" "$tag" </dev/null >"$TMP/unmigrate.out" 2>&1 || {
                tail -n 15 "$TMP/unmigrate.out"
                warn "undoing the migrations of '$name' failed (see above). The database schema is now BETWEEN the two"
                warn "versions, and the upgraded version is stopped, not removed. Either go forward again (the start of"
                warn "the new version applies the undone migrations again):"
                warn "           ./scripts/upgrade.sh run --resume --no-pull"
                warn "or go back with the database dump:"
                dump_way_back >&2
                exit 1
            }
        done <<<"$undo"
    fi

    if [ "$from" != "$head" ]; then
        step "Switching the source tree to $(short "$from")"
        g checkout --quiet --detach "$from" || die "git checkout failed (see above). ${stopped:+The containers are stopped, not removed. }Fix what git says and run 'rollback' again, or go forward: ./scripts/upgrade.sh run --resume --no-pull"
        # From here on the files of this script may be gone from disk; it runs from memory.
        if [ ! -f "$SCRIPT_DIR/upgrade.sh" ]; then
            SCRIPT_GONE="Revision $(short "$from") does not have this script, so deploy/scripts/upgrade.sh is gone from disk now. To get it back (and run 'rollback' again): cd $top && git checkout ${branch:-<branch>}"
        fi
    fi
    step "Pointing the image names back at the saved images"
    while IFS='|' read -r name id tag; do
        [ -n "$name" ] || continue
        case "$name" in *@*) continue ;; esac # pinned by digest: it never moved
        docker image tag "$id" "$name" || die "cannot tag $id as $name"
        say "  $name = $(short "${id#sha256:}")"
    done <<<"$(state_all IMAGE)"

    WANT=$(dc config --services 2>/dev/null | sort | tr '\n' ' ')
    [ -n "${WANT// /}" ] || die "the compose file of $(short "$from") is not valid with today's .env (docker compose config)"
    step "Bringing the previous layout up: $WANT"
    remove_stale_containers
    dc up -d --remove-orphans --no-build || die "docker compose up failed (see above)"

    step "Waiting for the services (up to $TIMEOUT s)"
    wait_healthy || die "the previous version is running but not healthy; see 'docker compose logs --tail 100'"
    if [ -n "$(stale_containers)" ]; then
        die "containers that do not belong to this layout are left: $(stale_containers | tr '\n' ' ')"
    fi
    # the next `run` reads from here which revision is deployed
    write_state "STATUS=rolled-back" "" "DEPLOYED_REV=$from" "DEPLOYED_PANEL_IMAGE=$(docker inspect -f '{{.Image}}' "$(service_container panel)" 2>/dev/null)"

    step "Rolled back"
    say "  revision: $(describe_revision "$from")$([ "$from" != "$head" ] && printf ' (detached HEAD)')"
    show_containers
    say ""
    if [ "$from" != "$to" ] || [ -n "$SCRIPT_GONE" ]; then
        say "The source tree now is the old version$([ -n "$SCRIPT_GONE" ] && printf ', which does not have this script: deploy/scripts/upgrade.sh is gone from disk')."
        say "'git checkout ${branch:-<branch>}' brings the new source tree$([ -n "$SCRIPT_GONE" ] && printf ' and this script') back. To try the upgrade again later:"
        say "  cd $top && git checkout ${branch:-<branch>} && cd deploy && ./scripts/upgrade.sh run$(again_flags)"
    fi
}

# --- restore-db -----------------------------------------------------------------------------------
# Runs inside the database container, with the dump on standard input. $1: suffix of the two
# database names, $2: "drop-public" when the dump creates the schema "public" itself.
# The dump is plain SQL without DROP statements (pg_dump without --clean), so it is loaded into a
# NEW, empty database in one transaction; only when all of it went in, the two databases swap
# names. The database as it was stays, under the name <db>_before_restore_<suffix>.
# shellcheck disable=SC2016 # the script runs inside the container
RESTORE_SCRIPT='
set -u
db=$POSTGRES_DB
new="${db}_restore_$1"
old="${db}_before_restore_$1"
admin=postgres
[ "$db" = postgres ] && admin=template1
q() { psql -X -q -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" "$@"; }
q -d "$admin" -c "CREATE DATABASE \"$new\" TEMPLATE template0" || { echo "cannot create the database $new" >&2; exit 3; }
if [ "$2" = drop-public ]; then q -d "$new" -c "DROP SCHEMA IF EXISTS public" || exit 3; fi
if ! q --single-transaction -d "$new" -f - >/dev/null; then
    q -d "$admin" -c "DROP DATABASE IF EXISTS \"$new\""
    echo "the dump did not load (see above); the database $db was not changed" >&2
    exit 4
fi
tables=$(q -d "$new" -c "SELECT count(*) FROM django_migrations" 2>/dev/null) || tables=0
if [ "${tables:-0}" -lt 1 ]; then
    q -d "$admin" -c "DROP DATABASE IF EXISTS \"$new\""
    echo "the dump holds no MCPanel database (no django_migrations rows); the database $db was not changed" >&2
    exit 4
fi
swapped=no
for try in 1 2 3 4 5; do # a connection that came in between (a health check) is closed and the swap tried again
    if q -d "$admin" >/dev/null <<SQL
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('"'"'$db'"'"', '"'"'$new'"'"') AND pid <> pg_backend_pid();
BEGIN;
ALTER DATABASE "$db" RENAME TO "$old";
ALTER DATABASE "$new" RENAME TO "$db";
COMMIT;
SQL
    then
        swapped=yes
        break
    fi
    sleep 1
done
if [ "$swapped" != yes ]; then
    echo "the databases could not be swapped (see above): $db was not changed, and $new holds the dump" >&2
    exit 5
fi
echo "$old"
'

cmd_restore_db() {
    preflight
    local file="$RESTORE_FILE" pg db others kept mode=keep-public suffix
    [ -n "$file" ] || die "restore-db needs the dump file: ./scripts/upgrade.sh restore-db <upgrade-backup-....sql> [--yes]"
    if [ ! -f "$file" ] || [ ! -r "$file" ]; then
        die "$file is not a readable file"
    fi
    head -n 5 "$file" | grep -q -F 'PostgreSQL database dump' || die "$file does not look like a plain SQL dump of pg_dump (as 'run' writes them)"
    grep -q -x 'CREATE SCHEMA public;' "$file" && mode=drop-public
    pg=$(service_container postgres running) || die "the database container is not running. Start it alone: docker compose up -d postgres"
    # shellcheck disable=SC2016 # expanded inside the container
    db=$(docker exec "$pg" sh -c 'printf %s "$POSTGRES_DB"' 2>/dev/null) || db=""
    [ -n "$db" ] || die "cannot read the database name from the database container"
    others=$(other_services)
    suffix=${STAMP//-/_}

    say "MCPanel stack \"$PROJECT\" in $DEPLOY_DIR"
    say "  dump:      $file ($(wc -c <"$file" | tr -d ' ') bytes)"
    say "  database:  $db in $(docker inspect -f '{{.Name}}' "$pg" 2>/dev/null | cut -c2-)"
    say "  The dump is loaded into a new database. When all of it went in, that one becomes \"$db\" and the"
    say "  database as it is now is kept as \"${db}_before_restore_$suffix\". Everything the panel wrote after the"
    say "  dump was taken is not in it. Login sessions are never part of a dump: everybody logs in again."
    [ -f "${file%.sql}.env" ] && say "  ${file%.sql}.env is the .env of that moment: its secret key fits what the dump holds."
    if [ -n "$others" ]; then
        refuse "these services use the database and must be stopped first: $others
           docker compose stop $others
         Nothing was changed."
    fi
    if [ "$YES" != 1 ]; then
        refuse "nothing was changed. To do it: ./scripts/upgrade.sh restore-db $file --yes"
    fi

    step "Loading the dump into a new database"
    kept=$(docker exec -i "$pg" sh -c "$RESTORE_SCRIPT" sh "$suffix" "$mode" <"$file") || die "the restore failed (see above)"
    kept=$(tail -n 1 <<<"$kept")
    step "Done"
    say "  \"$db\" now holds the dump. The database as it was is kept as \"$kept\"; when all is well, remove it:"
    say "      docker compose exec postgres sh -c 'dropdb -U \"\$POSTGRES_USER\" $kept'"
    say "  Next: ./scripts/upgrade.sh rollback (to finish going back), or docker compose up -d"
}

main() {
    local command="${1:-}"
    [ $# -gt 0 ] && shift
    while [ $# -gt 0 ]; do
        case "$1" in
            --force) FORCE=1 ;;
            --with-migrations) WITH_MIGRATIONS=1 ;;
            --resume) RESUME=1 ;;
            --no-pull) NO_PULL=1 ;;
            --no-build) NO_BUILD=1 ;;
            --no-backup) NO_BACKUP=1 ;;
            --dry-run) DRY=1 ;;
            --yes) YES=1 ;;
            --from)
                [ $# -ge 2 ] || die "--from needs a revision"
                FROM_OPTION="$2"
                shift
                ;;
            --timeout)
                [ $# -ge 2 ] || die "--timeout needs a number of seconds"
                TIMEOUT="$2"
                shift
                ;;
            -h | --help) command=help ;;
            -*) die "unknown option $1 (see ./scripts/upgrade.sh help)" ;;
            *)
                if [ "$command" != restore-db ] || [ -n "$RESTORE_FILE" ]; then
                    die "unknown option $1 (see ./scripts/upgrade.sh help)"
                fi
                RESTORE_FILE="$1"
                ;;
        esac
        shift
    done
    case "$TIMEOUT" in '' | *[!0-9]*) die "--timeout needs a number of seconds" ;; esac
    case "$command" in
        status) cmd_status ;;
        dry-run)
            DRY=1
            cmd_run
            ;;
        run) cmd_run ;;
        rollback) cmd_rollback ;;
        restore-db) cmd_restore_db ;;
        help | "")
            usage
            [ -n "$command" ] || return 64
            ;;
        *)
            usage >&2
            return 64
            ;;
    esac
}

# One line on purpose: bash has read the whole file before anything runs, so `rollback` can
# replace or remove this file (git checkout) while it is running.
main "$@"; exit $?
