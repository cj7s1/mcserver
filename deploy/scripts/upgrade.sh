#!/bin/bash
# Upgrade the MCPanel stack of this checkout, or take the last upgrade back.
#
#   cd <mcpanel source>/deploy
#   ./scripts/upgrade.sh status      what runs now, what the source tree wants, the rollback point
#   ./scripts/upgrade.sh dry-run     everything `run` would do; changes nothing
#   ./scripts/upgrade.sh run         build the version of the source tree and switch to it
#   ./scripts/upgrade.sh rollback    back to the version and images from before the last `run`
#
# The usual upgrade:
#
#   cd <mcpanel source> && git pull
#   cd deploy && ./scripts/upgrade.sh dry-run && ./scripts/upgrade.sh run
#
# What `run` does, in this order:
#   1. refuses while a job is queued or running (asks the database; --force skips this)
#   2. records the rollback point: the git revision that is deployed, the image of every
#      running container (tagged mcpanel-rollback/<name>:<time>, so no prune removes it) and
#      the state of the database migrations
#   3. dumps the database (without login sessions) and copies .env next to it, both mode 600
#   4. pulls the upstream images (database) and rebuilds panel and agent on fresh base images
#   5. refuses if the new version changes the database schema, unless --with-migrations
#   6. stops and removes containers of services the new version no longer has (for example
#      worker, worker-periodic, beat, redis) with 2 minutes to finish, then
#      `docker compose up -d --remove-orphans`
#   7. waits until every service is running and healthy, checks that nothing of the old layout
#      is left and prints what changed
# Minecraft server containers are never touched: they do not belong to the Compose project.
#
# `rollback` checks out the recorded revision (detached HEAD), points the image names back at
# the saved images, undoes the database migrations of the upgrade and brings that layout up.
#
# Options:
#   --force             run, rollback: do not refuse because of queued or running jobs
#   --with-migrations   run: the new version may change the database schema
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
SUPERVISE=/usr/local/bin/mcpanel-supervise.py

FORCE=0
WITH_MIGRATIONS=0
FROM_OPTION=""
NO_PULL=0
NO_BUILD=0
NO_BACKUP=0
TIMEOUT=300
DRY=0
TMP=""
PROJECT=""
WANT=""      # services of the source tree's compose file, space separated
FROM_REV=""  # the revision that is deployed now ("" = unknown / nothing deployed)
FROM_WHY=""
TO_REV=""
TO_LABEL=""

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'upgrade: %s\n' "$*" >&2; }
die() {
    printf 'upgrade: %s\n' "$*" >&2
    exit 1
}
refuse() {
    printf 'upgrade: %s\n' "$*" >&2
    exit 2
}

usage() {
    say "usage: ./scripts/upgrade.sh {status|dry-run|run|rollback} [--force] [--with-migrations] [--from REV]"
    say "                            [--no-pull] [--no-build] [--no-backup] [--timeout SECONDS] [--dry-run]"
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
    trap 'rm -rf "$TMP"' EXIT
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

state_all() { # state_all KEY -> every value, one per line
    local key value
    [ -f "$STATE_FILE" ] || return 0
    while IFS='=' read -r key value; do
        [ "$key" = "$1" ] && printf '%s\n' "$value"
    done <"$STATE_FILE"
    return 0
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
    FROM_REV=""
    FROM_WHY="unknown"
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
    done < <(state_all IMAGE)
    entry=$(state_get BACKUP_SQL) && [ -n "$entry" ] && say "  database dump: $entry"
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
    local id service state files
    say "MCPanel stack \"$PROJECT\" in $DEPLOY_DIR"
    if [ -n "$FROM_REV" ]; then
        say "  from:  $(describe_revision "$FROM_REV")   [$FROM_WHY]"
    else
        say "  from:  $FROM_WHY"
    fi
    say "  to:    $(describe_revision "$TO_REV")$([ "$TO_LABEL" = "$TO_REV" ] || printf '   [with local changes]')"
    if [ -n "$FROM_REV" ] && [ "$FROM_REV" != "$TO_REV" ]; then
        say "  commits: $(g rev-list --count "$FROM_REV..$TO_REV" 2>/dev/null || printf '?') new, $(g rev-list --count "$TO_REV..$FROM_REV" 2>/dev/null || printf '?') dropped"
    elif [ -n "$FROM_REV" ]; then
        say "  same revision: images are rebuilt on fresh base images"
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
    files=""
    [ "$NO_BUILD" = 1 ] || files=$(new_migration_files)
    if [ -n "$files" ]; then
        say "  database migrations in this version (run needs --with-migrations):"
        while IFS= read -r id; do say "    $id"; done <<<"$files"
    fi
}

require_known_origin() {
    [ -z "$(project_containers)" ] && return 0
    [ -n "$FROM_REV" ] && return 0
    die "cannot tell which git revision the running containers were built from: no image label, no record of
         an earlier run, and neither the source tree nor ORIG_HEAD has a compose file with exactly the services
         that are running ($(running_services)). Find it with 'git log --oneline' and pass it: --from <revision>"
}

record_rollback_point() {
    local id service state name image_id tag repo seen="" lines="" obsolete="" entry old
    # tags of older rollback points go away once this run has succeeded
    while IFS='|' read -r name image_id tag; do
        [ -n "$tag" ] && obsolete="$obsolete $tag"
    done < <(state_all IMAGE)
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
    write_state "STATUS=started" "$lines"
}

write_state() { # write_state STATUS=... [MORE LINES]
    local tmp="$STATE_FILE.tmp.$$" body="${2:-}"
    if [ -z "$body" ] && [ -f "$STATE_FILE" ]; then
        body=$(grep -v -e '^#' -e '^STATUS=' -e '^DEPLOYED_' "$STATE_FILE")$'\n'
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
        die "the database dump failed; nothing was changed"
    }
    mv -f "$sql.part" "$sql"
    chmod 600 "$sql" "$env"
    say "  $sql ($(wc -c <"$sql" | tr -d ' ') bytes, without login sessions)"
    say "  $env (copy of .env: its secret key decrypts what the dump holds; keep both private)"
    printf 'BACKUP_SQL=%s\nBACKUP_ENV=%s\n' "$sql" "$env" >>"$STATE_FILE"
    # keep the newest few (the names sort by time)
    files=("$BACKUP_DIR"/upgrade-backup-[0-9]*-[0-9]*.sql)
    for ((index = 0; index < ${#files[@]} - KEEP_BACKUPS; index++)); do
        [ -f "${files[index]}" ] || continue
        rm -f "${files[index]}" "${files[index]%.sql}.env"
        say "  removed the old backup ${files[index]##*/}"
    done
}

check_new_migrations() {
    local plan
    if ! service_container postgres running >/dev/null; then
        say "  the database is not running: migrations are applied at the first start"
        return 0
    fi
    if dc run --rm --no-deps -T panel manage migrate --check >"$TMP/migrate.out" 2>&1; then
        say "  none: the database schema stays as it is"
        return 0
    fi
    plan=$(dc run --rm --no-deps -T panel manage showmigrations --plan 2>/dev/null | grep -F '[ ]')
    if [ -z "$plan" ]; then
        tail -n 5 "$TMP/migrate.out"
        die "could not ask the new version for its database migrations; nothing was changed (the old version is still running)"
    fi
    say "  the new version changes the database schema:"
    while IFS= read -r line; do say "   $line"; done <<<"$plan"
    if [ "$WITH_MIGRATIONS" != 1 ]; then
        refuse "refusing without --with-migrations. Nothing was changed: the old version is still running.
         A rollback will undo these migrations again (data written into new columns is lost then)."
    fi
    say "  --with-migrations: they are applied when the new panel starts; 'rollback' undoes them"
    printf 'MIGRATED=yes\n' >>"$STATE_FILE"
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
        count=$(docker exec "$id" sh -c 'n=0; for f in /proc/[0-9]*/cmdline; do if tr "\0" " " <"$f" 2>/dev/null | grep -q "celery -A mcpanel [b]eat"; then n=$((n+1)); fi; done; echo "$n"' 2>/dev/null) || count=0
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
    done < <(state_all IMAGE)
}

cmd_run() {
    preflight
    find_deployed_revision
    find_target_revision
    show_plan
    [ "$NO_BUILD" = 1 ] || require_known_origin
    say ""
    check_jobs "before the switch"

    if [ "$DRY" = 1 ]; then
        step "docker compose --dry-run up -d --remove-orphans (nothing is changed)"
        local line previous=""
        while IFS= read -r line; do # compose prints every event twice
            case "$line" in "$previous" | *" Running "* | *" Running" | "") continue ;; esac
            say "  $line"
            previous=$line
        done < <(dc --dry-run up -d --remove-orphans 2>&1)
        say ""
        show_rollback_point
        say ""
        say "Nothing was changed. To do it: ./scripts/upgrade.sh run$([ "$NO_BUILD" = 1 ] && printf ' --no-build')"
        return 0
    fi

    local had_containers=0 branch panel_image old
    [ -n "$(project_containers)" ] && had_containers=1
    branch=$(g symbolic-ref -q --short HEAD) || branch=""

    if [ "$NO_BUILD" = 1 ]; then
        apply_configuration
        return 0
    fi

    step "Recording the rollback point"
    if [ "$had_containers" = 1 ]; then
        record_rollback_point
        printf 'RECORDED=%s\nFROM_REV=%s\nTO_REV=%s\nBRANCH=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$FROM_REV" "$TO_REV" "$branch" >>"$STATE_FILE"
        step "Backing up the database"
        backup_database
    else
        say "  nothing is running: there is nothing to go back to"
    fi

    export MCPANEL_REVISION="$TO_LABEL"
    if [ "$NO_PULL" = 1 ]; then
        step "Building panel and agent (with the base images on this host)"
        dc build || die "the build failed; nothing was changed (the old version is still running)"
    else
        step "Pulling upstream images"
        dc pull --ignore-buildable || die "pulling failed; nothing was changed (the old version is still running). Without internet: --no-pull"
        step "Building panel and agent on fresh base images (docker compose build --pull)"
        dc build --pull || die "the build failed; nothing was changed (the old version is still running)"
    fi

    step "Database migrations of the new version"
    check_new_migrations

    step "Switching over"
    check_jobs "right before the switch"
    [ "$had_containers" = 1 ] && write_state "STATUS=switching"
    remove_stale_containers
    dc up -d --remove-orphans || {
        warn "docker compose up failed. See the messages above."
        [ "$had_containers" = 1 ] && warn "To go back: ./scripts/upgrade.sh rollback"
        exit 1
    }

    step "Waiting for the services (up to $TIMEOUT s)"
    if ! wait_healthy; then
        warn "the new version is running but not healthy."
        [ "$had_containers" = 1 ] && warn "Look at 'docker compose logs --tail 100 panel'; to go back: ./scripts/upgrade.sh rollback"
        exit 1
    fi

    step "Checking the layout"
    if ! verify_layout; then
        warn "the layout is not as expected (see above)."
        [ "$had_containers" = 1 ] && warn "To go back: ./scripts/upgrade.sh rollback"
        exit 1
    fi

    step "Done"
    if [ "$had_containers" = 1 ]; then
        panel_image=$(docker inspect -f '{{.Image}}' "$(service_container panel)" 2>/dev/null)
        write_state "STATUS=done" "" "DEPLOYED_REV=$TO_REV" "DEPLOYED_PANEL_IMAGE=$panel_image"
        if [ "$FROM_REV" != "$TO_REV" ]; then
            say "  revision: $(describe_revision "$FROM_REV")"
            say "        ->  $(describe_revision "$TO_REV")"
        else
            say "  revision: unchanged ($(short "$TO_REV")), images rebuilt"
        fi
        show_changes
        for old in $(state_all OBSOLETE_TAG); do
            docker image rm "$old" >/dev/null 2>&1 && say "  dropped the older rollback tag $old"
        done
        (umask 077 && grep -v '^OBSOLETE_TAG=' "$STATE_FILE" >"$STATE_FILE.tmp.$$") && mv -f "$STATE_FILE.tmp.$$" "$STATE_FILE"
        say "  to go back: ./scripts/upgrade.sh rollback"
    else
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
migrations_to_undo() { # lines "<app> <target>" (target "zero" for apps that had no migration)
    local now app name recorded found
    now=$(migration_state) || return 1
    while read -r app name; do
        [ -n "$app" ] || continue
        found=""
        while read -r recorded; do
            if [ "${recorded%% *}" = "$app" ]; then
                found=${recorded#* }
                break
            fi
        done < <(state_all MIGRATION)
        if [ -z "$found" ]; then
            printf '%s zero\n' "$app"
        elif [ "$found" != "$name" ]; then
            printf '%s %s\n' "$app" "$found"
        fi
    done <<<"$now"
}

cmd_rollback() {
    preflight
    local status from to branch name id tag undo="" head service state ids="" line
    status=$(state_get STATUS) || die "there is no rollback point ($STATE_FILE does not exist)"
    case "$status" in
        switching | done) ;;
        started) die "the last run stopped before it changed anything: the old version is still running, there is nothing to roll back" ;;
        rolled-back) die "the last upgrade was rolled back already (to upgrade again: git checkout <branch>, then ./scripts/upgrade.sh run)" ;;
        *) die "unknown state '$status' in $STATE_FILE" ;;
    esac
    from=$(state_get FROM_REV)
    to=$(state_get TO_REV)
    branch=$(state_get BRANCH) || branch=""
    [ -n "$from" ] || die "the rollback point has no revision"
    g cat-file -e "$from^{commit}" 2>/dev/null || die "revision $from is not in this checkout any more"
    head=$(g rev-parse HEAD)

    say "MCPanel stack \"$PROJECT\" in $DEPLOY_DIR"
    say "  now:      $(describe_revision "$head")"
    say "  back to:  $(describe_revision "$from")   [recorded $(state_get RECORDED)]"
    while IFS='|' read -r name id tag; do
        [ -n "$name" ] || continue
        docker image inspect "$id" >/dev/null 2>&1 || die "the saved image of $name ($id) is gone: this rollback point cannot be used"
        say "  image:    $name -> $(short "${id#sha256:}")"
    done < <(state_all IMAGE)
    if [ "$from" != "$head" ] && ! g diff --quiet HEAD -- 2>/dev/null; then
        die "the source tree has local changes; git cannot switch to $(short "$from") safely. Commit or stash them first."
    fi
    if [ "$(state_get MIGRATIONS_RECORDED)" = yes ]; then
        if undo=$(migrations_to_undo); then
            if [ -n "$undo" ]; then
                say "  database migrations to undo:"
                while read -r line; do say "    $line"; done <<<"$undo"
            fi
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

    if [ -n "$undo" ]; then
        step "Undoing the database migrations of the upgrade"
        # nothing may use the new schema while it is taken away: stop everything but the database
        while read -r id service state; do
            [ "$service" = postgres ] || ids="$ids $id"
        done < <(project_containers)
        for id in $ids; do docker stop -t "$STOP_SECONDS" "$id" >/dev/null & done
        wait
        while read -r name tag; do
            say "  $name -> $tag"
            dc run --rm --no-deps -T panel manage migrate "$name" "$tag" >"$TMP/unmigrate.out" 2>&1 || {
                tail -n 15 "$TMP/unmigrate.out"
                warn "this migration cannot be undone automatically. The upgraded version is stopped, not removed:"
                warn "  go on with the new version:   docker compose up -d"
                warn "  or restore the database dump: $(state_get BACKUP_SQL || printf 'none was taken')"
                exit 1
            }
        done <<<"$undo"
    fi

    if [ "$from" != "$head" ]; then
        step "Switching the source tree to $(short "$from")"
        g checkout --quiet --detach "$from" || die "git checkout failed; the containers were not changed$([ -n "$undo" ] && printf ' (but they are stopped: docker compose up -d starts the new version again)')"
    fi
    # From here on the files of this script may be gone from disk; it runs from memory.
    step "Pointing the image names back at the saved images"
    while IFS='|' read -r name id tag; do
        [ -n "$name" ] || continue
        case "$name" in *@*) continue ;; esac # pinned by digest: it never moved
        docker image tag "$id" "$name" || die "cannot tag $id as $name"
        say "  $name = $(short "${id#sha256:}")"
    done < <(state_all IMAGE)

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
    if [ "$from" != "$to" ]; then
        say "The source tree now is the old version. To try the upgrade again later:"
        say "  cd $(g rev-parse --show-toplevel 2>/dev/null || printf '<mcpanel source>') && git checkout ${branch:-<branch>} && cd deploy && ./scripts/upgrade.sh run"
    fi
}

main() {
    local command="${1:-}"
    [ $# -gt 0 ] && shift
    while [ $# -gt 0 ]; do
        case "$1" in
            --force) FORCE=1 ;;
            --with-migrations) WITH_MIGRATIONS=1 ;;
            --no-pull) NO_PULL=1 ;;
            --no-build) NO_BUILD=1 ;;
            --no-backup) NO_BACKUP=1 ;;
            --dry-run) DRY=1 ;;
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
            *) die "unknown option $1 (see ./scripts/upgrade.sh help)" ;;
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
