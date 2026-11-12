#!/bin/bash
# Daily copy of the MCPanel database off the cache drive (Unraid, User Scripts plugin).
#
#   backup-db.sh [--project NAME] [--env FILE] [--keep N] [DEST_DIR]
#
# Runs pg_dump inside the postgres container of the Compose project (default "mcpanel"), leaves
# out the login sessions (django_session) and writes the dump gzip-compressed to DEST_DIR
# (default /mnt/user/Backups/mcpanel-db), next to a copy of deploy/.env: the secret key in it
# decrypts what the dump holds (RCON passwords, API keys), and AGENT_RESTIC_PASSWORD opens the
# server backups. Both are private: the folder is 0700, the files 0600. The newest N dumps
# (default 14) are kept, older ones removed with their .env copy.
#
# deploy/.env is found through the postgres container (Compose records the folder of the
# project in the label com.docker.compose.project.working_dir), else next to this script
# (../.env); --env names it.
#
# In User Scripts: add a script with a line that runs it from the MCPanel checkout
#   bash /mnt/user/appdata/mcpanel-src/deploy/unraid/backup-db.sh /mnt/user/Backups/mcpanel-db
# and schedule it daily (a copy pasted into User Scripts gets no fixes from later updates).
#
# Root runs it. It refuses (FAILED, nothing written) while group or others can write this script,
# its folder or the .env it copies: whoever can change them (an SMB user, a container that mounts
# the folder) could run code as root here. The message names the repair (in a checkout:
# chmod -R go-w <checkout>).
#
# The last line of the output always says how it went:
#   mcpanel-db backup: OK ...      exit status 0
#   mcpanel-db backup: FAILED ...  exit status 1 (nothing half-written is left behind)
#
# Restore: deploy/scripts/upgrade.sh restore-db <file> (it reads plain SQL: gunzip the file first).
#
# One run at a time per DEST_DIR (a lock folder .mcpanel-db.lock holds the pid of the run; a lock
# whose process is gone is taken over); two runs in the same second get different file names (_2, ...).
set -u
set -o pipefail

PROJECT=mcpanel
DEST=/mnt/user/Backups/mcpanel-db
KEEP=14
ENV_FILE=""
PREFIX=mcpanel-db

status() {
    printf 'mcpanel-db backup: %s\n' "$*"
}
fail() {
    status "FAILED: $*"
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --project)
            [ $# -ge 2 ] || fail "--project needs a name"
            PROJECT=$2
            shift 2
            ;;
        --project=*)
            PROJECT=${1#--project=}
            shift
            ;;
        --env)
            [ $# -ge 2 ] || fail "--env needs a file"
            ENV_FILE=$2
            shift 2
            ;;
        --keep)
            [ $# -ge 2 ] || fail "--keep needs a number"
            KEEP=$2
            shift 2
            ;;
        -h | --help)
            sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        -*)
            fail "unknown option $1 (see --help)"
            ;;
        *)
            DEST=$1
            shift
            ;;
    esac
done

[[ "$PROJECT" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || fail "'$PROJECT' is not a Compose project name"
[[ "$KEEP" =~ ^[1-9][0-9]*$ ]] || fail "--keep must be a whole number of at least 1"
[ -n "$DEST" ] || fail "no destination folder"

# Who can change what root runs or copies here. loose_modes ABSOLUTE-PATH...: lines "<mode> <path>"
# of those that group or others can write (symbolic links followed).
loose_modes() {
    find -L "$@" -maxdepth 0 -perm /022 -printf '%m %p\n' 2>/dev/null
    return 0
}
HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd) || fail "cannot find the folder of $0"
TOP=$(CDPATH='' cd -- "$HERE/../.." 2>/dev/null && pwd) || TOP=""
# refuse_loose PATH...: FAILED while group or others can write one of them. In an MCPanel
# checkout the repair is a chmod of the whole checkout (a `git pull` under a loose umask rewrites
# more files than these); for paths outside it, of those paths.
refuse_loose() {
    local found mode path repair="" others=""
    found=$(loose_modes "$@")
    [ -n "$found" ] || return 0
    printf 'group or others can write to (mode, path):\n'
    while read -r mode path; do
        printf '  %s %s\n' "$mode" "$path"
        if [ -n "$TOP" ] && [ "$TOP/deploy/unraid" -ef "$HERE" ] && [[ "$path" == "$TOP"/* ]]; then
            printf -v repair 'chmod -R go-w %q' "$TOP"
        else
            printf -v others '%s %q' "$others" "$path"
        fi
    done <<<"$found"
    [ -z "$others" ] || repair="${repair:+$repair && }chmod go-w$others"
    printf 'Whoever can change these (an SMB user, a container that mounts the folder) could run code as root here.\n'
    fail "group or others can write to what root runs or copies here (listed above); repair it as root, then run this again: $repair"
}
refuse_loose "$HERE/$(basename -- "$0")" "$HERE"
case "$DEST" in
    /*) ;;
    *) fail "the destination must be an absolute path (got '$DEST')" ;;
esac
command -v docker >/dev/null 2>&1 || fail "docker is not installed or not on PATH"
command -v gzip >/dev/null 2>&1 || fail "gzip is not installed"

pg=$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" \
    --filter "label=com.docker.compose.service=postgres" --filter status=running 2>/dev/null | head -n 1)
[ -n "$pg" ] || fail "no running postgres container of the Compose project '$PROJECT' (is the stack up? see --project)"

if [ -z "$ENV_FILE" ]; then
    workdir=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$pg" 2>/dev/null)
    if [ -n "$workdir" ] && [ -f "$workdir/.env" ]; then
        ENV_FILE=$workdir/.env
    elif [ -f "$(dirname "$0")/../.env" ]; then
        ENV_FILE=$(dirname "$0")/../.env
    fi
fi
[ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ] && [ -r "$ENV_FILE" ] || fail "cannot find deploy/.env (pass it with --env FILE)"
refuse_loose "$(CDPATH='' cd -- "$(dirname -- "$ENV_FILE")" && pwd)/$(basename -- "$ENV_FILE")"

umask 077
mkdir -p "$DEST" 2>/dev/null || fail "cannot create $DEST"
chmod 700 "$DEST" 2>/dev/null || fail "cannot make $DEST private (chmod 700)"
[ -w "$DEST" ] || fail "cannot write to $DEST"

# one run at a time: mkdir is atomic, the pid inside says whose lock it is
lock="$DEST/.$PREFIX.lock"
take_lock() { mkdir "$lock" 2>/dev/null && printf '%s\n' "$$" >"$lock/pid"; }
if ! take_lock; then
    holder=$(cat "$lock/pid" 2>/dev/null)
    if [[ "$holder" =~ ^[0-9]+$ ]] && kill -0 "$holder" 2>/dev/null; then
        fail "another run (pid $holder) is writing to $DEST; try again when it has finished"
    fi
    # the run that held it is gone (killed): take it over
    rm -rf "$lock"
    take_lock || fail "another run is writing to $DEST (lock $lock)"
fi
part="" env_part="" err=""
cleanup() {
    rm -f ${part:+"$part"} ${env_part:+"$env_part"} ${err:+"$err"}
    rm -rf "$lock"
}
trap cleanup EXIT

# a run in the same second as the one before gets its own name (_2, _3, ...; sorts after it): nothing is overwritten
stamp=$(date +%Y%m%d-%H%M%S)
base=$stamp
n=1
while [ -e "$DEST/$PREFIX-$stamp.sql.gz" ] || [ -e "$DEST/$PREFIX-$stamp.env" ]; do
    n=$((n + 1))
    stamp="${base}_$n"
done
dump="$DEST/$PREFIX-$stamp.sql.gz"
env_copy="$DEST/$PREFIX-$stamp.env"
part="$dump.part.$$"
env_part="$env_copy.part.$$"
err="$DEST/.$PREFIX-$stamp.err.$$"

# shellcheck disable=SC2016  # the variables are expanded by the shell inside the container
if ! docker exec "$pg" sh -c 'pg_dump -U "$POSTGRES_USER" --exclude-table-data=django_session "$POSTGRES_DB"' 2>"$err" |
    gzip -c >"$part"; then
    why=$(head -n 3 "$err" 2>/dev/null | tr '\n' ' ')
    fail "pg_dump in container $pg did not finish${why:+: $why}"
fi
# a dump that pg_dump finished ends with this line; anything else is cut off
gzip -dc "$part" 2>/dev/null | tail -n 5 | grep -q -- '-- PostgreSQL database dump complete' ||
    fail "the dump is incomplete (no 'dump complete' line at its end)"
cp "$ENV_FILE" "$env_part" || fail "cannot copy $ENV_FILE"
chmod 600 "$part" "$env_part" || fail "cannot make the files private (chmod 600)"
mv -f "$env_part" "$env_copy" || fail "cannot write $env_copy"
mv -f "$part" "$dump" || {
    rm -f "$env_copy"
    fail "cannot write $dump"
}

# keep the newest KEEP dumps (the names sort by time) and their .env copies
removed=0
mapfile -t dumps < <(find "$DEST" -maxdepth 1 -type f -name "$PREFIX-[0-9]*-[0-9]*.sql.gz" -printf '%f\n' | sort)
excess=$((${#dumps[@]} - KEEP))
for ((i = 0; i < excess; i++)); do
    old=${dumps[i]}
    rm -f "$DEST/$old" "$DEST/${old%.sql.gz}.env" && removed=$((removed + 1))
done

size=$(wc -c <"$dump" | tr -d ' ')
kept=$((${#dumps[@]} - removed))
status "OK $dump ($size bytes, without login sessions) and ${env_copy##*/}; $kept dump(s) kept, $removed old one(s) removed"
exit 0
