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
#      `docker compose up -d --remove-orphans` (after it removed what an earlier `up` that was
#      cut off left half replaced; a service that `up` did not start is tried once more and is
#      a failure at once after that)
#   7. waits until every service is running and healthy, checks that nothing of the old layout
#      is left and prints what changed (the images of the containers that run now)
# Minecraft server containers are never touched: they do not belong to the Compose project.
# Until step 6 nothing is switched: a run that ends before it points the image names (for example
# mcpanel/panel:local) back at the images they had, and the old version keeps running.
#
# A `run` that did not finish (the build failed, the new version did not become healthy, it was
# killed) keeps its rollback point, and no later `run` makes a new one on top of it. If nothing
# was switched yet, the next `run` goes on with the recorded one. Otherwise `run` refuses until
# you choose: `rollback` (back to the old version) or `run --resume` (go on to the new one, with
# the rollback point as it was recorded).
#
# A terminal that is closed: before the switch (step 6) it ends the run, and nothing was changed.
# From the switch on, and in `rollback` and `restore-db` from their first change on, the script
# ignores the hangup and finishes without the terminal; `docker compose` then runs in a session
# of its own, so the hangup does not get to it either. What was printed is in the log (below).
# `status` names the log and says whether that command is still at work; while it is, another
# `run`, `rollback` or `restore-db` refuses. Ctrl-C and a kill still end the script (a kill -9 or
# a host that goes down cannot be caught): `status` then says what is left to do, and both ways
# out, `rollback` and `run --resume`, first remove what a `docker compose up` that was cut off
# left half-made.
#
# `rollback` first dumps the database as it is (like step 3 of `run`, same folder and names; not
# with --no-backup): from then on every failure names `restore-db <that dump> --yes` as the way
# back, and nothing the panel wrote since the upgrade is lost by it. It then checks out the recorded
# revision (detached HEAD), points the image names back at the saved images, undoes the database
# migrations of the upgrade and brings that layout up.
# It asks the upgraded version first whether every migration can be undone and stops nothing if
# one cannot. After the checkout this script may be gone from disk (older revisions do not have
# it); `git checkout <branch>` brings it back. With the old version running and the source tree
# on the branch again, do not run `docker compose up -d` by hand: the compose file of the tree
# does not fit the images that run (`status` says so whenever the two differ).
#
# Going back to a release from before multi-game (it knows only Minecraft servers; its database has
# no `game` column): undoing the migrations would drop what a server of another game is (game,
# ports, settings, secrets, setup state), the old panel would take such a server for a Minecraft
# server, and a UDP and a TCP server that share a port number make undoing them fail. So before it
# stops anything (also with --dry-run) `rollback` asks the upgraded version
# (`manage rollback_other_games`) and refuses while such servers exist, listing them and the shared
# port numbers. With --archive-other-games it first stops each of them through the running panel
# (its Stop job: the game saves), archives it (its Archive job), disables its schedules and copies
# its row into the table mcpanel_rollback_other_games, which no migration touches; a server that
# shares its port number gets a number above 65535 while the old version runs. Undoing migration
# instances 0007 parks these servers on a node of their own whose agent URL leads nowhere ("parked
# by upgrade.sh rollback (other games)"): the old panel takes them for Minecraft servers, and there
# every start (also after an Unarchive) fails before anything is changed. The next `run` puts it
# all back (migration instances 0007: their own node again) and leaves the servers archived.
#
# `restore-db FILE` loads a dump written by step 3 into a new database and, when that worked,
# swaps it with the current one, which is kept under another name. Everything but the database
# container must be stopped. It is the way back when a migration cannot be undone. It ends with
# the commands that start the stack again; which ones depends on where the last run ended.
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
#   --no-backup         run: skip the database dump; rollback: skip its own dump (see below)
#   --timeout SECONDS   run, rollback: how long to wait for healthy (default 300)
#   --dry-run           rollback: only show what it would do
#   --archive-other-games
#                       rollback: archive the servers of games other than Minecraft first (see above)
#   --yes               restore-db: do it (without it the command only says what it would do)
#
# Files it writes, all in this deploy directory, mode 600 and ignored by git:
#   upgrade-state.env                   the rollback point (do not edit)
#   upgrade-backup-<time>.sql / .env    database dump and copy of .env, by `run` and by `rollback` (the
#                                       newest 3 are kept, and always the two the state file names)
#   upgrade-log-<time>.log              what a `run`, `rollback` or `restore-db` printed, written
#                                       from its first change on (a command that refuses or only
#                                       shows something leaves none; the newest 10 are kept)
#
# Who can change this checkout: root runs this script, builds the panel and the agent (which gets
# the Docker socket) from the checkout and runs git in it. `run`, `rollback` and `restore-db --yes`
# refuse, before they change anything, while group or others can write a file or folder that git
# tracks here, the checkout's own folder, its .git folder, .git/config, .git/hooks or deploy/.env;
# `status` and the dry runs only warn. The message names the repair (`chmod -R go-w <checkout>`).
# git runs without fsmonitor and hooks, and everything the script writes gets umask 022 or stricter.
# Files that a `git pull` rewrites get the umask of the shell that pulls: `(umask 022 && git pull)`.
#
# Needs bash, docker with the compose plugin, and git. Nothing else.
set -uo pipefail
# Whatever the umask of the shell (with 000 every file a `git checkout` of `rollback` rewrites would
# be writable by everybody). Files with secrets are written under umask 077 where they are made.
umask 022

# The standard error the script was started with. Messages go there (warn), also from a trap that
# runs while the output of a command is redirected into a file.
exec 4>&2

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd) || exit 1
DEPLOY_DIR=$(dirname -- "$SCRIPT_DIR")
STATE_FILE="$DEPLOY_DIR/upgrade-state.env"
BACKUP_DIR="${MCPANEL_UPGRADE_BACKUP_DIR:-$DEPLOY_DIR}"
KEEP_BACKUPS=3
KEEP_LOGS=10
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
ARCHIVE_OTHER=0   # rollback --archive-other-games
GAMES_UNDO=""     # rollback: "--undo app=target ..." when the undo takes away what the multi-game release added
GAMES_ARCHIVE=0   # rollback: ... and servers of other games (or shared port numbers) must be archived first
GAMES_ARCHIVED=0  # rollback: ... and that was done
GUARD_REFUSAL="IRREVERSIBLE (other games)" # migrations instances 0005/0006 in `migrate --plan` (_other_games_guard.py)
RESTORE_FILE=""
TMP=""
PROJECT=""
WANT=""      # services of the source tree's compose file, space separated
FROM_REV=""  # the revision that is deployed now ("" = unknown / nothing deployed)
FROM_WHY=""
PANEL_RUNS=0 # find_deployed_revision: a panel container is running
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
LOG_WANTED=0      # run, rollback, restore-db (not their dry forms): what is printed goes into a log as well
LOG_FILE=""       # ... this file, from the first change on (begin_log)
LOG_HELD=""       # ... and what was printed before that waits here
LOG_ARGS=""       # the command line, for the first line of the log
DETACH=""         # set at the first change of a switch (hold_on): how `docker compose` is started from then on
UP_PROBLEM=""     # bring_up: why the stack is not up
ROLLBACK_DUMP=""  # rollback: the dump it took at its start ...
WAY_BACK=""       # ... and the line every failure from then on ends with
RUNNING_PID=""    # another_runs: the command of this script that is still at work, ...
RUNNING_LOG=""    # ... its log ...
RUNNING_WHAT=""   # ... and "<command line> | started <time>"
LOOSE_REPAIR=""   # check_modes: the command that makes the checkout right, when group or others can write it

# Everything the script prints goes through say and warn, and with a log into the log as well.
log_line() {
    if [ -n "$LOG_FILE" ]; then
        printf '%s\n' "$1" >>"$LOG_FILE"
    elif [ "$LOG_WANTED" = 1 ]; then
        LOG_HELD="$LOG_HELD$1"$'\n'
    fi
}
say() {
    printf '%s\n' "$*"
    log_line "$*"
}
step() {
    STEP=$*
    say ""
    say "==> $*"
}
warn() {
    printf 'upgrade: %s\n' "$*" >&4
    log_line "upgrade: $*"
}
die() {
    warn "$*"
    way_back_note "$*"
    [ -z "$SCRIPT_GONE" ] || warn "$SCRIPT_GONE"
    exit 1
}
refuse() {
    warn "$*"
    way_back_note "$*"
    exit 2
}
way_back_note() { # after the dump of a rollback: its way back, unless MESSAGE names it already
    [ -n "$WAY_BACK" ] || return 0
    case "$1" in *"$ROLLBACK_DUMP"*) return 0 ;; esac
    warn "$WAY_BACK"
}

# The output of a command that belongs on the screen: line by line to the screen and into the
# log. It reads to the end of that output whatever the terminal sends meanwhile (a command whose
# reader went away would be ended by its next line).
relay() {
    local line
    (
        trap '' HUP INT TERM PIPE
        if [ -n "$LOG_FILE" ]; then exec 9>>"$LOG_FILE"; else exec 9>/dev/null; fi
        while IFS= read -r line || [ -n "$line" ]; do
            printf '%s\n' "$line"
            printf '%s\n' "$line" >&9
        done
    )
}
shown() { # shown COMMAND...: run it with its output on the screen (and in the log); its status is the command's
    local status
    if [ -z "$LOG_FILE" ] && [ -z "$DETACH" ]; then
        "$@"
        return
    fi
    if [ -n "$DETACH" ]; then
        # After hold_on the command is out of reach of Ctrl-C and of a kill of the process group
        # (dc). The part of the pipeline that waits for it must not go before it does: bash would
        # run the trap at once then, and the script would end while the command is still at work.
        {
            trap '' INT TERM
            "$@"
        } 2>&1 | relay
        status=${PIPESTATUS[0]}
    else
        "$@" 2>&1 | relay
        status=${PIPESTATUS[0]}
    fi
    return "$status"
}
show_file() { # show_file FILE [LINES]: the last lines of a file a command wrote its output into
    local line
    while IFS= read -r line; do say "$line"; done < <(tail -n "${2:-10}" "$1" 2>/dev/null)
}

usage() {
    say "usage: ./scripts/upgrade.sh {status|dry-run|run|rollback} [--force] [--with-migrations] [--resume] [--from REV]"
    say "                            [--no-pull] [--no-build] [--no-backup] [--timeout SECONDS] [--dry-run]"
    say "                            [--archive-other-games]"
    say "       ./scripts/upgrade.sh restore-db FILE [--yes]"
    say "The comment at the top of $0 explains every step."
}

# docker compose in the deploy directory. After hold_on it runs in a session of its own: the
# hangup of a closed terminal, Ctrl-C and a kill of the process group do not get to it, so it
# always finishes what it began (a `docker compose up` that is cut off leaves containers half
# replaced). The subshell that waits for it ignores Ctrl-C and a kill meanwhile: the script acts
# on them when the command has ended.
dc() {
    case "$DETACH" in
        setsid) (
            trap '' INT TERM
            cd "$DEPLOY_DIR" && setsid -w docker compose "$@" </dev/null
        ) ;;
        nohup) (cd "$DEPLOY_DIR" && nohup docker compose "$@" </dev/null) ;;
        *) (cd "$DEPLOY_DIR" && docker compose "$@") ;;
    esac
}
# git runs as root here: no fsmonitor command and no hooks, whatever .git/config or .git/hooks hold.
GIT_SAFE=(-c core.fsmonitor= -c core.hooksPath=/dev/null)
g() { git "${GIT_SAFE[@]}" -C "$DEPLOY_DIR" "$@"; }

# --- who can change what root runs ----------------------------------------------------------------
# Whoever can write the files root runs and builds from (an SMB user, a container that mounts the
# folder) could run code as root on this host. The check covers what git tracks (and the folders
# that hold it), the checkout's own folder, .git, .git/config, .git/hooks and deploy/.env.
TOP_DIR=$(dirname -- "$DEPLOY_DIR")

loose_modes() { # lines "<mode> <path>" (relative to TOP_DIR) of what group or others can write
    local -a paths=(./. ./.git ./.git/config ./.git/hooks)
    local -A seen=()
    local file dir mode name
    while IFS= read -r -d '' file; do
        paths+=("./$file")
        dir=$file
        while [[ "$dir" == */* ]]; do
            dir=${dir%/*}
            [ -z "${seen[$dir]:-}" ] || break
            seen[$dir]=1
            paths+=("./$dir")
        done
    done < <(git "${GIT_SAFE[@]}" -C "$TOP_DIR" ls-files -z 2>/dev/null)
    (
        cd "$TOP_DIR" || exit 0
        # A symbolic link is skipped: what it points to is checked on its own. deploy/.env is
        # followed (-L), wherever it points.
        find "${paths[@]}" -maxdepth 0 ! -type l -perm /022 -printf '%m %p\n' 2>/dev/null
        [ ! -e deploy/.env ] || find -L ./deploy/.env -maxdepth 0 -perm /022 -printf '%m %p\n' 2>/dev/null
    ) | while read -r mode name; do
        printf '%s %s\n' "$mode" "${name#./}"
    done
}

check_modes() { # check_modes refuse|warn
    local found line count=0 shown="" repair
    found=$(loose_modes)
    [ -n "$found" ] || return 0
    while IFS= read -r line; do
        count=$((count + 1))
        [ "$count" -gt 8 ] || shown="$shown
           $line"
    done <<<"$found"
    [ "$count" -le 8 ] || shown="$shown
           ... and $((count - 8)) more"
    printf -v repair 'chmod -R go-w %q' "$TOP_DIR"
    # chmod -R leaves the target of a symbolic link alone; named on the command line it is changed
    [ ! -L "$DEPLOY_DIR/.env" ] || printf -v repair '%s && chmod go-w %q' "$repair" "$DEPLOY_DIR/.env"
    LOOSE_REPAIR=$repair # the dry runs end with it, before the command they name
    if [ "$1" = refuse ]; then
        refuse "refusing: group or others can write to $count file(s) or folder(s) of this checkout (mode, path):$shown
         Root runs this script, builds the panel and the agent (Docker socket) from these files and runs
         git here: whoever can change them (an SMB user, a container that mounts the folder) could run
         code as root on this host. Nothing was changed. Repair it (as root), then run this command again:
           $repair
         To keep it that way, pull with: (umask 022 && git pull)"
    fi
    warn "WARNING: group or others can write to $count file(s) or folder(s) of this checkout (mode, path):$shown
         'run', 'rollback' and 'restore-db' refuse until it is repaired (as root):
           $repair
         To keep it that way, pull with: (umask 022 && git pull)"
}

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
# The last line of the log says how the command ended (`status` reads it).
# shellcheck disable=SC2317,SC2329 # called by the EXIT trap (SC2317: shellcheck up to 0.10, SC2329: from 0.11)
on_exit() {
    local status=$?
    [ "$PHASE" = "before-switch" ] && restore_image_names
    [ -z "$LOG_FILE" ] || printf '# ended %s, exit status %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$status" >>"$LOG_FILE"
    [ -z "$TMP" ] || rm -rf "$TMP"
}

# Ctrl-C, a kill, or (until hold_on) a closed terminal session: note it in the state file, so
# `status` and the next `run` say that the run did not finish and why. The message goes to the
# standard error the script was started with (warn), not into the file the output of the command
# that was running is redirected to.
# shellcheck disable=SC2317,SC2329 # called by the signal traps
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
    way_back_note ""
    [ -z "$SCRIPT_GONE" ] || warn "$SCRIPT_GONE"
    exit $((128 + $2))
}

# From the first change of a switch on (run, rollback, restore-db) a closed terminal must not end
# the work half-way. The script ignores the hangup, and a reader of its output that went away;
# every command it starts inherits that. `docker compose` is also started in a session of its
# own (dc), where the hangup of the terminal does not get to it at all; without setsid, nohup
# takes that place.
# Tried with Docker 29.9 and Compose 5.7 (SIGHUP to the process group while `docker compose up -d`
# replaced a container): as it is, Compose dies between creating the replacement and removing the
# container it replaces. Started with SIGHUP ignored (inherited, or by nohup) it finishes, and
# so it does in a session of its own (setsid), where the signal never arrives.
hold_on() {
    trap '' HUP PIPE
    [ -z "$DETACH" ] || return 0
    if setsid -w true >/dev/null 2>&1; then
        DETACH="setsid"
    elif command -v nohup >/dev/null 2>&1; then
        DETACH="nohup"
    else
        DETACH="inherit"
    fi
}

# --- the log ------------------------------------------------------------------------------
newest_log() { # the log that was written to last, or fails
    local file newest=""
    for file in "$DEPLOY_DIR"/upgrade-log-*.log; do
        [ -f "$file" ] || continue
        if [ -z "$newest" ] || [ "$file" -nt "$newest" ]; then newest=$file; fi
    done
    [ -n "$newest" ] && printf '%s\n' "$newest"
}

# Is the command that wrote the newest log still at work? (It goes on after its terminal was
# closed, so a second one must not start next to it.) Sets RUNNING_PID, RUNNING_LOG, RUNNING_WHAT.
another_runs() {
    local first last
    RUNNING_LOG=$(newest_log) || return 1
    IFS= read -r first <"$RUNNING_LOG" || return 1
    # (the line that says how it ended need not be the very last: output of a command that was
    # interrupted can still arrive after it)
    last=$(grep '^# ended ' "$RUNNING_LOG" | tail -n 1)
    [ -z "$last" ] || return 1
    case "$first" in "# pid "*" | "*" | started "*) ;; *) return 1 ;; esac
    RUNNING_PID=${first#"# pid "}
    RUNNING_PID=${RUNNING_PID%% *}
    RUNNING_WHAT=${first#*" | "}
    case "$RUNNING_PID" in '' | *[!0-9]*) return 1 ;; esac
    [ "$RUNNING_PID" != "$$" ] || return 1
    if [ -d "/proc/$$" ]; then # a process of that number that is this script (the number may have been given out again)
        [ -r "/proc/$RUNNING_PID/cmdline" ] && tr '\0' ' ' <"/proc/$RUNNING_PID/cmdline" 2>/dev/null | grep -q 'upgrade\.sh'
    else
        kill -0 "$RUNNING_PID" 2>/dev/null
    fi
}

refuse_while_another_runs() {
    another_runs || return 0
    refuse "another command of this script is still at work (pid $RUNNING_PID: ${RUNNING_WHAT%" | started "*}, started ${RUNNING_WHAT##*" | started "}).
         It goes on when its terminal was closed. Nothing was changed by this one. Wait until the other has ended;
         to follow it:   tail -f $RUNNING_LOG
         './scripts/upgrade.sh status' says whether it is still at work and how it ended."
}

# From here on the command changes things: what it prints goes into a log from now on, with
# everything it printed so far. After a closed terminal the log says how it went on and ended.
begin_log() {
    local file="$DEPLOY_DIR/upgrade-log-$STAMP.log" count oldest
    if [ "$LOG_WANTED" != 1 ] || [ -n "$LOG_FILE" ]; then
        return 0
    fi
    refuse_while_another_runs
    [ ! -e "$file" ] || file="$DEPLOY_DIR/upgrade-log-$STAMP-$$.log"
    if ! (umask 077 && printf '# pid %s | ./scripts/upgrade.sh %s | started %s\n%s' "$$" "$LOG_ARGS" "$(date '+%Y-%m-%d %H:%M:%S')" "$LOG_HELD" >"$file") 2>/dev/null; then
        LOG_WANTED=0
        LOG_HELD=""
        warn "cannot write $file: this command keeps no log"
        return 0
    fi
    LOG_HELD=""
    LOG_FILE=$file
    say ""
    say "Log: $file"
    say "     (what this command prints, from its first line on; 'status' says how it ended)"
    while :; do # keep the newest few
        count=0
        oldest=""
        for file in "$DEPLOY_DIR"/upgrade-log-*.log; do
            [ -f "$file" ] || continue
            count=$((count + 1))
            if [ -z "$oldest" ] || [ "$file" -ot "$oldest" ]; then oldest=$file; fi
        done
        [ "$count" -gt "$KEEP_LOGS" ] || break
        rm -f "$oldest" || break
    done
}

show_log() { # status: the newest log and how the command that wrote it ended
    local file first last what
    file=$(newest_log) || return 0
    IFS= read -r first <"$file" || first=""
    what=${first#*" | "}
    last=$(grep '^# ended ' "$file" | tail -n 1)
    say "Log:          $file"
    case "$first" in
        "# pid "*" | "*" | started "*) say "              ${what%" | started "*}, started ${what##*" | started "}" ;;
    esac
    if another_runs; then
        say "              STILL AT WORK (pid $RUNNING_PID): wait until it has ended. To follow it: tail -f $file"
    else
        case "$last" in
            "# ended "*) say "              ${last#"# "}" ;;
            *) say "              it does not say how it ended and its process is gone: it was killed, or the host went down" ;;
        esac
    fi
}

# --- what is there ----------------------------------------------------------------------
preflight() {
    command -v docker >/dev/null 2>&1 || die "docker is not installed"
    docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing (docker compose version)
         On Unraid (which ships none): bash $DEPLOY_DIR/unraid/array-start.sh --compose-bin <your docker-compose binary>
         links it now and at every array start (docs/UNRAID.md, section 1)."
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

stopped_panel() { # the id of the panel container, when there is exactly one and it ran and is stopped
    local id service state found="" count=0
    while read -r id service state; do
        [ "$service" = panel ] || continue
        count=$((count + 1))
        case "$state" in exited | dead) found=$id ;; esac
    done < <(project_containers)
    [ "$count" = 1 ] && [ -n "$found" ] && printf '%s\n' "$found"
}

stale_containers() { # containers of services the source tree's compose file does not have
    local id service state
    # shellcheck disable=SC2086 # WANT is a space separated list
    while read -r id service state; do
        in_list "$service" $WANT || printf '%s %s %s\n' "$id" "$service" "$state"
    done < <(project_containers)
}

# A `docker compose up` that was cut off while it replaced a container (a kill, a host that went
# down) leaves the replacement behind under the temporary name Compose gives it,
# <12 hex digits>_<project>-<service>-<n>, next to the stopped container it was to replace. The
# next `up` then ends without an error and without starting that service, or fails with
# "dependency failed to start" (seen with Compose 5.7). So before every `up`:
#   a replacement that does not run is removed (Compose makes a new one);
#   a replacement that runs is the container of its service: it gets the proper name, and a
#   stopped container that has that name is removed.
clear_half_recreated() {
    local id service state name proper other other_state line list=""
    while read -r id service state; do
        name=$(docker inspect -f '{{.Name}}' "$id" 2>/dev/null) || continue
        list="${list}${id} ${state} ${name#/}"$'\n'
    done < <(project_containers)
    while read -r id state name; do
        [[ "$name" =~ ^[0-9a-f]{12}_("$PROJECT"-.+)$ ]] || continue
        proper=${BASH_REMATCH[1]}
        if [ "$state" != running ]; then
            if docker rm "$id" >/dev/null 2>"$TMP/rm.err"; then
                say "  removed $name ($state): the replacement container an interrupted 'docker compose up' left behind"
            else
                warn "could not remove the half-made container $name: $(head -n 1 "$TMP/rm.err")"
            fi
            continue
        fi
        other=""
        other_state=""
        while read -r line; do
            [ "${line##* }" = "$proper" ] || continue
            other=${line%% *}
            other_state=${line#* }
            other_state=${other_state%% *}
        done <<<"$list"
        if [ "$other_state" = running ]; then
            warn "$name and $proper both run: left as they are for 'docker compose up'"
            continue
        fi
        if [ -n "$other" ]; then
            if ! docker rm "$other" >/dev/null 2>"$TMP/rm.err"; then
                warn "could not remove the stopped container $proper: $(head -n 1 "$TMP/rm.err")"
                continue
            fi
            say "  removed the stopped container $proper ($other_state): $name runs in its place"
        fi
        if docker rename "$id" "$proper" 2>"$TMP/rm.err"; then
            say "  renamed $name to $proper (an interrupted 'docker compose up' left it with its temporary name)"
        else
            warn "could not rename $name to $proper: $(head -n 1 "$TMP/rm.err")"
        fi
    done <<<"$list"
}

# The services of the layout that have no started container, as " panel(created) beat(no container)".
# `docker compose up` can end without an error and still leave a service like that.
not_started() {
    local service id other state found list
    list=$(project_containers)
    for service in $WANT; do
        found="no container"
        while read -r id other state; do
            [ "$other" = "$service" ] || continue
            case "$state" in
                created | exited | dead) [ "$found" = started ] || found=$state ;;
                *) found=started ;;
            esac
        done <<<"$list"
        [ "$found" = started ] || printf ' %s(%s)' "$service" "$found"
    done
}

# `docker compose up -d --remove-orphans` for the layout of the checkout, and the look whether
# every service got started: if not, once more, and then it is a failure at once (nothing to wait
# 5 minutes for). Fails with the reason in UP_PROBLEM.
bring_up() { # bring_up [MORE OPTIONS OF up]
    local attempt missing=""
    UP_PROBLEM=""
    for attempt in 1 2; do
        clear_half_recreated
        if ! shown dc up -d --remove-orphans "$@"; then
            UP_PROBLEM="docker compose up failed (see the messages above)"
            return 1
        fi
        missing=$(not_started)
        [ -n "$missing" ] || return 0
        [ "$attempt" = 2 ] || say "  docker compose up ended without an error, but did not start:$missing. Once more:"
    done
    UP_PROBLEM="docker compose up ended without an error, twice, and still did not start:$missing
         ('docker compose ps -a' lists the containers, 'docker compose logs --tail 50 <service>' shows what one wrote)"
    return 1
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
    local count=0 id line value="" has_line=0 them are="containers are"
    while read -r id; do
        [ -n "$id" ] && count=$((count + 1))
    done < <(docker ps -a --filter "label=$SERVER_LABEL" --filter status=exited --filter status=created --filter status=dead --format '{{.ID}}' 2>/dev/null)
    [ "$count" -gt 0 ] || return 0
    them="these $count containers"
    if [ "$count" = 1 ]; then
        them="this container"
        are="container is"
    fi
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
            0 | [Ff]alse | [Nn]o | [Oo]ff) say "Stopped servers: $count exited server $are kept on this host ($RETIRE_SETTING=$value in .env)." ;;
        esac
        return 0
    fi
    say "Stopped servers: this host has $count exited Minecraft server ${are% *}. A stopped server needs no"
    say "  container any more: shortly after the upgrade the panel removes $them. World data is"
    say "  not touched, the last 2,000 console lines of each are kept, and a stopped server is started from the"
    say "  panel as before (its container is created again). To look first, add this line to .env BEFORE the run:"
    say "      $RETIRE_SETTING=false"
    say "  Nothing is removed then, and after the upgrade this lists what would be removed, without removing anything:"
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
    # The panel container that runs says it. Without one, the only panel container there is does
    # (the stack is stopped); two of them, or one that was made and never started, say nothing:
    # that is a switch that was cut off.
    PANEL_RUNS=1
    if ! panel=$(service_container panel running); then
        PANEL_RUNS=0
        panel=$(stopped_panel) || panel=""
    fi
    if [ -n "$panel" ]; then
        label=$(docker inspect -f "{{index .Config.Labels \"$REVISION_LABEL\"}}" "$panel" 2>/dev/null)
        label=${label%-dirty}
        if [[ "$label" =~ ^[0-9a-f]{7,40}$ ]] && FROM_REV=$(g rev-parse --verify -q "$label^{commit}"); then
            FROM_WHY="label of the running panel image"
            [ "$PANEL_RUNS" = 1 ] || FROM_WHY="label of the image of the panel container, which is stopped"
            return 0
        fi
        image=$(docker inspect -f '{{.Image}}' "$panel" 2>/dev/null)
        if [ -n "$image" ] && [ "$(state_get DEPLOYED_PANEL_IMAGE)" = "$image" ] && FROM_REV=$(g rev-parse --verify -q "$(state_get DEPLOYED_REV)^{commit}"); then
            FROM_WHY="recorded by the last run of this script"
            return 0
        fi
    fi
    # A run that ended before its switch changed nothing: what it recorded is what is deployed.
    # (`run` does not come here then: choose_origin reads that record itself.)
    if [ "$(state_get STATUS)" = started ] && record_matches_containers && FROM_REV=$(g rev-parse --verify -q "$(state_get FROM_REV)^{commit}"); then
        FROM_WHY="recorded $(state_get RECORDED || printf '?') by the run that ended before the switch"
        return 0
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
    local id service state info mark name health image image_id row
    printf -v row '  %-16s %-26s %-9s %-10s %s' SERVICE CONTAINER STATE HEALTH IMAGE
    say "$row"
    while read -r id service state; do
        info=$(docker inspect -f '{{.Name}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}|{{.Config.Image}}|{{.Image}}' "$id" 2>/dev/null) || continue
        IFS='|' read -r name health image image_id <<<"$info"
        mark=""
        # shellcheck disable=SC2086
        in_list "$service" $WANT || mark="   <- not in the source tree's layout: removed by 'run'"
        printf -v row '  %-16s %-26s %-9s %-10s %s (%s)%s' "$service" "${name#/}" "$state" "$health" "$image" "$(short "${image_id#sha256:}")" "$mark"
        say "$row"
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
    if unfinished "$status" && another_runs; then
        say "  A command of this script is still at work on it (pid $RUNNING_PID, see 'Log' above): wait until it has ended."
    elif unfinished "$status"; then
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
    if [ "$PANEL_RUNS" != 1 ] && [ -n "$(project_containers)" ]; then
        # never the label of a container that does not run (one that was only made, for example)
        if [ -n "$FROM_REV" ]; then
            say "Deployed:     the panel is not running. $(describe_revision "$FROM_REV")   [$FROM_WHY]"
        else
            say "Deployed:     the panel is not running$(unfinished "$(state_get STATUS)" && printf ', and the last run or rollback did not finish (see the rollback point below)')"
        fi
    elif [ -n "$FROM_REV" ]; then
        say "Deployed:     $(describe_revision "$FROM_REV")   [$FROM_WHY]"
    else
        say "Deployed:     $FROM_WHY$(unfinished "$(state_get STATUS)" && printf ' (the last run or rollback did not finish: see the rollback point below)')"
    fi
    if [ -n "$FROM_REV" ] && [ "$FROM_REV" != "$TO_REV" ]; then
        say "              The source tree is another version than the deployed one: do not run 'docker compose up -d'"
        say "              here (the compose file of the tree does not fit the images that run). 'run' upgrades to the"
        say "              source tree; 'git checkout --detach $(short "$FROM_REV")' is the checkout that fits what runs."
    fi
    if [ "$branch" != "(detached)" ] && upstream=$(g rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null); then
        remote_rev=$(run_limited 15 git "${GIT_SAFE[@]}" -C "$DEPLOY_DIR" ls-remote "${upstream%%/*}" "refs/heads/${upstream#*/}" 2>/dev/null)
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
        if service_container agent >/dev/null && ! service_container agent running >/dev/null; then
            # a server network removed while Docker was down leaves the agent's endpoint behind (FX-1)
            say "  The agent is not running. If it does not start with \"network ... not found\" (it was killed while a"
            say "  server ran and that server's network is gone), recreate it: docker compose up -d --force-recreate agent"
        fi
    fi
    say ""
    show_supervisor
    if jobs=$(active_jobs 2>/dev/null); then
        say "Jobs: ${jobs% *} queued, ${jobs#* } running"
    else
        say "Jobs: unknown (the database is not running or has no tables yet)"
    fi
    show_log
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

backup_database() { # backup_database [KEY WHAT-IS-TRUE-ON-FAILURE]: KEY BACKUP (run) or ROLLBACK
    local key=${1:-BACKUP} unchanged=${2:-$UNCHANGED} pg sql="$BACKUP_DIR/upgrade-backup-$STAMP.sql" env="$BACKUP_DIR/upgrade-backup-$STAMP.env" files index keep
    if [ "$NO_BACKUP" = 1 ]; then
        say "  skipped (--no-backup)"
        return 0
    fi
    if ! pg=$(service_container postgres running); then
        say "  the database is not running: nothing to dump"
        return 0
    fi
    # never over an earlier dump: a `rollback` (or a second run) in the same second as the run before it would
    # otherwise replace the dump from before the upgrade, the way back, with one of the upgraded database
    if [ -e "$sql" ] || [ -e "$env" ] || [ -e "$sql.part" ]; then
        sql="$BACKUP_DIR/upgrade-backup-$STAMP-$$.sql" env="$BACKUP_DIR/upgrade-backup-$STAMP-$$.env"
    fi
    (
        umask 077
        docker exec "$pg" sh -c 'pg_dump -U "$POSTGRES_USER" --exclude-table-data=django_session "$POSTGRES_DB"' >"$sql.part" 2>"$TMP/dump.err" &&
            cp "$DEPLOY_DIR/.env" "$env" 2>>"$TMP/dump.err"
    ) || {
        rm -f "$sql.part"
        show_file "$TMP/dump.err"
        die "the database dump failed (see above); $unchanged"
    }
    mv -f "$sql.part" "$sql"
    chmod 600 "$sql" "$env"
    say "  $sql ($(wc -c <"$sql" | tr -d ' ') bytes, without login sessions)"
    say "  $env (copy of .env: its secret key decrypts what the dump holds; keep both private)"
    state_edit "${key}_SQL ${key}_ENV" "${key}_SQL=$sql" "${key}_ENV=$env"
    [ "$key" = ROLLBACK ] && ROLLBACK_DUMP=$sql
    # keep the newest few (the names sort by time), and the dumps the state file names
    keep=" $(state_get BACKUP_SQL) $(state_get ROLLBACK_SQL) "
    files=("$BACKUP_DIR"/upgrade-backup-[0-9]*-[0-9]*.sql)
    for ((index = 0; index < ${#files[@]} - KEEP_BACKUPS; index++)); do
        [ -f "${files[index]}" ] || continue
        case "$keep" in *" ${files[index]} "*) continue ;; esac
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

# shellcheck disable=SC2317,SC2329 # called by on_exit, which the EXIT trap calls
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
        show_file "$TMP/migrate.out" 5
        die "could not ask the new version for its database migrations (see above); $UNCHANGED"
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
    docker rm $ids >/dev/null 2>"$TMP/rm.err" || die "could not remove the old containers:$names ($(head -n 1 "$TMP/rm.err"))"
    say "  removed$names"
}

wait_healthy() {
    local deadline=$((SECONDS + TIMEOUT)) pending service id state last=""
    while :; do
        pending=""
        for service in $WANT; do
            if ! id=$(service_container "$service" running) && ! id=$(service_container "$service"); then
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
                id=$(service_container "$service" running) || id=$(service_container "$service") || continue
                state=$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}ok{{end}}' "$id" 2>/dev/null)
                case "$state" in running/healthy | running/ok) continue ;; esac
                say "  --- $service: last health check"
                docker inspect -f '{{if .State.Health}}{{range .State.Health.Log}}{{.Output}}{{end}}{{end}}' "$id" 2>/dev/null | tail -n 3 | relay
                say "  --- $service: last log lines"
                docker logs --tail 25 "$id" 2>&1 | cut -c1-300 | relay
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

# Old and new image per image name. The old one is the one of the rollback point; the new one is
# read from the containers that are running, not from the image name: a name can point at an image
# no container was made from (a build that ended after the containers were made).
show_changes() {
    local name old tag now id service state used="" seen line ids told=""
    while read -r id service state; do
        [ "$state" = running ] || continue
        name=$(docker inspect -f '{{.Config.Image}}' "$id" 2>/dev/null) || continue
        now=$(docker inspect -f '{{.Image}}' "$id" 2>/dev/null) || continue
        used="${used}${name}|${now}|${service}"$'\n'
    done < <(project_containers)
    while IFS='|' read -r name old tag; do
        [ -n "$name" ] || continue
        ids=""
        while IFS='|' read -r seen now service; do
            # shellcheck disable=SC2086 # a space separated list of image ids
            [ "$seen" = "$name" ] && ! in_list "$now" $ids && ids="$ids $now"
        done <<<"$used"
        [ -n "$ids" ] || say "  $name: $(short "${old#sha256:}") -> no running container uses it any more"
        for now in $ids; do
            if [ "$now" = "$old" ]; then
                say "  $name: unchanged ($(short "${old#sha256:}"))"
            else
                say "  $name: $(short "${old#sha256:}") -> $(short "${now#sha256:}")"
            fi
        done
    done <<<"$(state_all IMAGE)"
    # an image name the rollback point does not have (a service that is new in this layout)
    line=$(state_all IMAGE)
    while IFS='|' read -r seen now service; do
        [ -n "$seen" ] || continue
        case $'\n'"$line" in *$'\n'"$seen|"*) continue ;; esac
        # shellcheck disable=SC2086 # a space separated list of image names
        in_list "$seen" $told && continue
        told="$told $seen"
        say "  $seen: new, $(short "${now#sha256:}")"
    done <<<"$used"
}

cmd_run() {
    preflight
    [ "$DRY" = 1 ] || refuse_while_another_runs
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
        if another_runs; then
            say "Nothing was changed. 'run' would refuse now: another command of this script is still at work"
            say "  (pid $RUNNING_PID: ${RUNNING_WHAT%" | started "*}). To follow it: tail -f $RUNNING_LOG"
        elif [ -n "$BLOCKED" ]; then
            say "Nothing was changed. 'run' would refuse now:"
            say "  $BLOCKED"
        else
            say "Nothing was changed. To do it: ${LOOSE_REPAIR:+$LOOSE_REPAIR && }$(next_command)"
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

    begin_log
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
    PHASE="before-switch"
    if [ "$NO_PULL" = 1 ]; then
        step "Building panel and agent (with the base images on this host)"
        shown dc build || die "the build failed; $UNCHANGED"
    else
        step "Pulling upstream images"
        shown dc pull --ignore-buildable || die "pulling failed; $UNCHANGED. Without internet: --no-pull"
        step "Building panel and agent on fresh base images (docker compose build --pull)"
        shown dc build --pull || die "the build failed; $UNCHANGED"
    fi

    step "Database migrations of the new version"
    check_new_migrations

    step "Switching over"
    check_jobs "right before the switch"
    # From here on the stack changes, and a closed terminal no longer ends the run (hold_on).
    hold_on
    PHASE="switching"
    [ "$point" = 1 ] && write_state "STATUS=switching"
    remove_stale_containers
    bring_up || {
        warn "$UP_PROBLEM."
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
        panel_image=$(docker inspect -f '{{.Image}}' "$(service_container panel running)" 2>/dev/null)
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
    say ""
    sign_in_note
}

# The last words of a run (text only). Since the version of the security review a session belongs to
# the browser tab that signed in: a session from an older version is refused once. Tabs left open run
# the old version's code until they are reloaded.
sign_in_note() {
    if [ -z "$FROM_REV" ] || ! g cat-file -e "$FROM_REV:backend/apps/core/session_binding.py" 2>/dev/null; then
        say "Everyone signs in once now: since this version a session belongs to the browser tab that signed in."
    fi
    say "Panel tabs that were open during the upgrade: reload them (they still run the old version's code)."
}

# After a rollback to a version without per-server networks (no agent/mcagent/pernet.py): text only.
# Its agent is not attached to those networks; a server on one keeps running, but the agent cannot
# reach its control port until the server was stopped and started once.
network_rollback_note() { # network_rollback_note REVISION
    g cat-file -e "$1:agent/mcagent/pernet.py" 2>/dev/null && return 0
    say "Servers on a network of their own (zz_mcpanel_i_...): the agent of this version is not attached to"
    say "those networks. Such a server keeps running and players can join it, but its console, its player list"
    say "and the save before a stop do not work until it was stopped and started once (this version then makes"
    say "its container on the shared network). A Minecraft server still saves by itself when it is stopped."
    say ""
}

# `run --no-build`: bring the containers in line with .env and the compose files, nothing else.
apply_configuration() {
    local image
    while IFS= read -r image; do # the locally built images must be there already: this mode never builds
        case "$image" in
            mcpanel/*) docker image inspect "$image" >/dev/null 2>&1 || die "the image $image does not exist yet: run without --no-build first" ;;
        esac
    done < <(dc config --images 2>/dev/null)
    begin_log
    step "Applying the configuration (no build, the rollback point is not touched)"
    check_jobs "right before the switch"
    hold_on
    remove_stale_containers
    bring_up --no-build || die "$UP_PROBLEM"
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
    if [ -n "$ROLLBACK_DUMP" ]; then
        # the dump this rollback took: nothing is lost with it
        others=$(other_services)
        printf '%s\n' "         The way back is the database dump this rollback took before it changed anything: nothing the" \
            "         panel wrote is lost (the servers of other games among it); server files are not touched:"
        [ -z "$others" ] || printf '           docker compose stop %s\n' "$others"
        printf '%s\n' "           ./scripts/upgrade.sh restore-db $ROLLBACK_DUMP --yes" \
            "         and then what restore-db says (it holds the schema of the upgraded version)."
        [ -z "$sql" ] || printf '%s\n' "         The dump from before the upgrade is older (what the panel wrote since is not in it):" \
            "           ./scripts/upgrade.sh restore-db $sql --yes, then ./scripts/upgrade.sh rollback"
        return 0
    fi
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

# The database dump as one choice of a refusal that changed nothing (no dump of the rollback yet).
dump_choice() {
    local sql others
    sql=$(state_get BACKUP_SQL) || sql=""
    if [ -z "$sql" ]; then
        printf '%s\n' "           - the database dump from before the upgrade is no choice: that run took none (--no-backup," \
            "             or the database was down)."
        return 0
    fi
    others=$(other_services)
    printf '%s\n' "           - or go back with the database dump from before the upgrade. Everything the panel wrote since" \
        "             then is lost, the servers of other games among it (they came with this version); server" \
        "             files are not touched:"
    [ -z "$others" ] || printf '               docker compose stop %s\n' "$others"
    printf '%s\n' "               ./scripts/upgrade.sh restore-db $sql --yes" \
        "               ./scripts/upgrade.sh rollback"
}

# Ask the upgraded version whether every migration can be undone: before anything is stopped, and
# once more when the list changed while the containers were stopping. Migrations instances 0005/0006
# answer "IRREVERSIBLE (other games)" while servers of other games are not archived and recorded:
# with --archive-other-games that is expected before the archive step (GAMES_ARCHIVE=1 and
# GAMES_ARCHIVED=0), and the plan is asked again after it.
check_undo_plan() { # check_undo_plan LINES ("<app> <target>") WHAT-IS-TRUE-NOW
    local name tag bad hint
    while read -r name tag; do
        [ -n "$name" ] || continue
        # </dev/null: `docker compose run` reads its standard input to the end, here the rest of this list
        if ! dc run --rm --no-deps -T panel manage migrate --plan "$name" "$tag" </dev/null >"$TMP/plan.out" 2>&1; then
            show_file "$TMP/plan.out"
            die "could not ask the upgraded version how to undo the migrations of '$name' (see above). $2"
        fi
        bad=$(grep -F IRREVERSIBLE "$TMP/plan.out")
        if [ "$GAMES_ARCHIVE" = 1 ] && [ "$GAMES_ARCHIVED" = 0 ]; then
            bad=$(grep -v -F "$GUARD_REFUSAL" <<<"$bad")
        fi
        bad=$(head -n 1 <<<"$bad")
        if [ -n "$bad" ]; then
            hint=""
            case "$bad" in
                *"$GUARD_REFUSAL"*) hint="
         (Servers of other games are not archived and recorded: ./scripts/upgrade.sh rollback --archive-other-games)" ;;
            esac
            refuse "the database migrations of '$name' cannot be undone (${bad#"${bad%%[![:space:]]*}"}). $2$hint
$(dump_way_back)"
        fi
    done <<<"$1"
    say "  asked the upgraded version: every one of them can be undone"
}

migration_number() { # migration_number NAME -> its number (0 for "zero" and for a name without one)
    local n=${1%%_*}
    case "$n" in '' | *[!0-9]*) n=0 ;; esac
    printf '%d' "$((10#$n))"
}

# The "--undo app=target" options of `manage rollback_other_games` when undoing LINES takes away
# what the multi-game release added: the game column (instances 0005) or the longer player
# names (metrics 0002). Prints nothing when it does not; fails when the database does not answer.
games_undo() { # games_undo LINES ("<app> <target>")
    local now app target name current need args=""
    now=$(migration_state) || return 1
    while read -r app target; do
        case "$app" in
            instances) need=5 ;;
            metrics) need=2 ;;
            *) continue ;;
        esac
        current=""
        while read -r name; do
            [ "${name%% *}" = "$app" ] && current=${name#* }
        done <<<"$now"
        if [ -n "$current" ] && [ "$(migration_number "$current")" -ge "$need" ] && [ "$(migration_number "$target")" -lt "$need" ]; then
            args="$args --undo $app=$target"
        fi
    done <<<"$1"
    printf '%s' "${args# }"
}

# Before anything is stopped (also in a dry run): does undoing LINES take away what servers of
# other games are, or bring back a unique index that servers sharing a port number break? The
# upgraded version answers (manage rollback_other_games, which lists them). Refuses unless
# --archive-other-games was given, and also then when a job holds such a server. Sets GAMES_UNDO
# and GAMES_ARCHIVE.
check_other_games() { # check_other_games LINES FROM-REVISION
    local code
    GAMES_UNDO=$(games_undo "$1") || die "the database does not answer. Nothing was changed."
    [ -n "$GAMES_UNDO" ] || return 0
    # shellcheck disable=SC2086 # a list of options
    dc run --rm --no-deps -T panel manage rollback_other_games $GAMES_UNDO </dev/null >"$TMP/games.out" 2>&1
    code=$?
    case "$code" in
        0)
            say "  servers of other games: none, and no port number is shared (asked the upgraded version)"
            return 0
            ;;
        3 | 4)
            # the command's own last line (Django's "CommandError: ...") says what this script says
            # next in its own words: a refusal, or that the archive step comes first
            grep -v '^CommandError: ' "$TMP/games.out" >"$TMP/games.shown"
            show_file "$TMP/games.shown" 400
            [ "$code" = 3 ] || refuse "the rollback cannot go on now (see above). Nothing was changed."
            ;;
        *)
            show_file "$TMP/games.out"
            die "could not ask the upgraded version about the servers of other games (see above). Nothing was changed."
            ;;
    esac
    if [ "$ARCHIVE_OTHER" = 1 ]; then
        GAMES_ARCHIVE=1
        if [ "$DRY" = 1 ]; then
            say "  --archive-other-games: 'rollback' would stop these servers through the running panel, archive and record"
            say "  them before it stops anything else."
        else
            say "  --archive-other-games: these servers are stopped through the panel, archived and recorded first."
        fi
        return 0
    fi
    refuse "the version the rollback goes back to ($(short "$2")) knows only Minecraft servers. Undoing the
         database migrations of this upgrade would remove what the servers listed above are (game, ports,
         settings, secrets, setup state), the old panel would take them for Minecraft servers, and port
         numbers that two servers share make undoing the migrations fail. Nothing was changed. Choose one:
           - keep the new version: nothing to do;
           - ./scripts/upgrade.sh rollback --archive-other-games
             stops these servers through the panel (each game saves first), archives them, disables their
             schedules and keeps what the rollback removes in the database (table mcpanel_rollback_other_games;
             a server that shares its port number gets a number above 65535 while the old version runs).
             While the old version runs they are parked on a node it cannot reach, so it cannot start
             them. The next './scripts/upgrade.sh run' puts it all back and leaves them archived;
$(dump_choice)"
}

# Stop, archive and record the servers of other games through the running panel (its own jobs).
archive_other_games() {
    local panel
    panel=$(service_container panel running) || {
        say "  the panel container is not running, and it is what stops and archives these servers: bring the"
        say "  upgraded version up first ('./scripts/upgrade.sh status' says how)"
        return 1
    }
    # shellcheck disable=SC2086 # a list of options
    shown docker exec "$panel" python manage.py rollback_other_games --archive $GAMES_UNDO </dev/null
}

cmd_rollback() {
    preflight
    [ "$DRY" = 1 ] || refuse_while_another_runs
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
                check_other_games "$undo" "$from"
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
        network_rollback_note "$from"
        say "Nothing was changed. To do it: ${LOOSE_REPAIR:+$LOOSE_REPAIR && }./scripts/upgrade.sh rollback$([ "$GAMES_ARCHIVE" = 1 ] && printf ' --archive-other-games')"
        return 0
    fi

    # From here on things change, and a closed terminal no longer ends the rollback (hold_on). A
    # rollback that does not get to its end is recorded as such: `run` then refuses to make a new
    # rollback point from a half-rolled-back stack.
    begin_log
    hold_on
    step "Dumping the database (the way back if this rollback does not get to its end)"
    backup_database ROLLBACK "nothing was changed (the upgraded version is still running)"
    if [ -n "$ROLLBACK_DUMP" ]; then
        WAY_BACK="the database as it was when this rollback began is in $ROLLBACK_DUMP. To put it back: ./scripts/upgrade.sh restore-db $ROLLBACK_DUMP --yes (it names what must be stopped first, and how to go on)."
    elif [ "$NO_BACKUP" = 1 ]; then
        say "  without it a rollback that fails has no way back but the dump from before the upgrade, which"
        say "  holds nothing the panel wrote since (servers of other games among it)."
    fi
    if [ "$GAMES_ARCHIVE" = 1 ]; then
        # With the stack still running: the panel stops each server through the agent. Until this
        # is done nothing else changes, and a failure leaves the upgraded version running as it was.
        step "Archiving the servers of other games (through the running panel)"
        archive_other_games || die "archiving the servers of other games did not finish (see above). Nothing was stopped
         and no migration was undone: the upgraded version keeps running. A server named above as archived
         stays archived (unarchive it in the panel to use it there). Fix what is said above, then run
         './scripts/upgrade.sh rollback --archive-other-games' again."
        GAMES_ARCHIVED=1
        # Undoing 0005/0006 must be possible now; nothing is stopped otherwise.
        check_undo_plan "$undo" "Nothing was stopped and no migration was undone: the upgraded version keeps running."
    fi
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
                show_file "$TMP/unmigrate.out" 15
                warn "undoing the migrations of '$name' failed (see above). The database schema is now BETWEEN the two"
                warn "versions, and the upgraded version is stopped, not removed. Either go forward again (the start of"
                warn "the new version applies the undone migrations again):"
                warn "           ./scripts/upgrade.sh run --resume --no-pull"
                warn "or go back with the database dump:
$(dump_way_back)"
                exit 1
            }
        done <<<"$undo"
    fi

    if [ "$from" != "$head" ]; then
        step "Switching the source tree to $(short "$from")"
        g checkout --quiet --detach "$from" 2>"$TMP/git.err" || {
            show_file "$TMP/git.err"
            die "git checkout failed (see above). ${stopped:+The containers are stopped, not removed. }Fix what git says and run 'rollback' again, or go forward: ./scripts/upgrade.sh run --resume --no-pull"
        }
        # From here on the files of this script may be gone from disk; it runs from memory.
        if [ ! -f "$SCRIPT_DIR/upgrade.sh" ]; then
            SCRIPT_GONE="Revision $(short "$from") does not have this script, so deploy/scripts/upgrade.sh is gone from disk now. To get it back (and run 'rollback' again): cd $top && git checkout ${branch:-<branch>}"
        fi
    fi
    step "Pointing the image names back at the saved images"
    while IFS='|' read -r name id tag; do
        [ -n "$name" ] || continue
        case "$name" in *@*) continue ;; esac # pinned by digest: it never moved
        docker image tag "$id" "$name" 2>"$TMP/tag.err" || die "cannot tag $id as $name ($(head -n 1 "$TMP/tag.err"))"
        say "  $name = $(short "${id#sha256:}")"
    done <<<"$(state_all IMAGE)"

    WANT=$(dc config --services 2>/dev/null | sort | tr '\n' ' ')
    [ -n "${WANT// /}" ] || die "the compose file of $(short "$from") is not valid with today's .env (docker compose config)"
    step "Bringing the previous layout up: $WANT"
    remove_stale_containers
    bring_up --no-build || die "$UP_PROBLEM"

    step "Waiting for the services (up to $TIMEOUT s)"
    wait_healthy || die "the previous version is running but not healthy; see 'docker compose logs --tail 100'"
    if [ -n "$(stale_containers)" ]; then
        die "containers that do not belong to this layout are left: $(stale_containers | tr '\n' ' ')"
    fi
    # the next `run` reads from here which revision is deployed
    write_state "STATUS=rolled-back" "" "DEPLOYED_REV=$from" "DEPLOYED_PANEL_IMAGE=$(docker inspect -f '{{.Image}}' "$(service_container panel running)" 2>/dev/null)"

    step "Rolled back"
    say "  revision: $(describe_revision "$from")$([ "$from" != "$head" ] && printf ' (detached HEAD)')"
    show_containers
    say ""
    if [ "$GAMES_ARCHIVE" = 1 ]; then
        say "The servers of other games are archived, and what they are is kept in the database (table"
        say "mcpanel_rollback_other_games). This version would take them for Minecraft servers, so they are parked"
        say "on the node 'parked by upgrade.sh rollback (other games)', which it cannot reach: a start (also after an"
        say "Unarchive) or a delete of one of them fails there and changes nothing. The next './scripts/upgrade.sh run'"
        say "puts them back on their node, still archived."
        say ""
    fi
    network_rollback_note "$from"
    if [ "$from" != "$to" ] || [ -n "$SCRIPT_GONE" ]; then
        # Which checkout he is on, and which command is safe in which checkout.
        say "The source tree ($top) is now at the old version, $(short "$from"), as a detached HEAD. It fits"
        say "what is running: in this checkout 'docker compose up -d' and the other docker compose commands are safe."
        [ -z "$SCRIPT_GONE" ] || say "That version does not have this script: deploy/scripts/upgrade.sh is gone from disk."
        say "'git checkout ${branch:-<branch>}' brings the new source tree$([ -n "$SCRIPT_GONE" ] && printf ' and this script') back. The tree is the NEW version then, while the OLD"
        say "one runs: do NOT run 'docker compose up -d' in that checkout (its compose file does not fit the old images,"
        say "the panel would not start). Use this script there; 'status' names the deployed version and the checkout"
        say "that fits it. To try the upgrade again later:"
        say "  cd $top && git checkout ${branch:-<branch>} && cd deploy && ./scripts/upgrade.sh run$(again_flags)"
        say "To come back to the checkout that fits the old version:"
        say "  cd $top && git checkout --detach $(short "$from")"
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

# The stack is stopped but for the database. What starts it again depends on which version is
# deployed, and that is what the state file of the last run says: after a restore the wrong
# command (a rollback that is done already, `docker compose up -d` with the compose file of the
# other version) must not be the one that is printed.
restore_next_steps() {
    local status from to branch head top what="run"
    status=$(state_get STATUS) || status=""
    from=$(state_get FROM_REV) || from=""
    to=$(state_get TO_REV) || to=""
    branch=$(state_get BRANCH) || branch=""
    head=$(g rev-parse HEAD 2>/dev/null) || head=""
    top=$(g rev-parse --show-toplevel 2>/dev/null) || top="<mcpanel source>"
    case "$status" in
        "")
            say "  Next: docker compose up -d"
            ;;
        done)
            say "  Next, to finish going back to the version from before the upgrade ($(short "$from")):"
            say "      ./scripts/upgrade.sh rollback"
            if [ "$head" = "$to" ]; then
                say "  To stay on the upgraded version with the data of the dump instead: docker compose up -d"
                say "  (the panel then applies the database migrations of the upgrade to the restored database when it starts)."
            fi
            ;;
        switching | rolling-back)
            [ "$status" = switching ] || what="rollback"
            say "  The last $what did not finish, so the stack is half old, half new: do NOT run 'docker compose up -d' now."
            say "  Next, one of:"
            say "      ./scripts/upgrade.sh rollback        back to the version from before the upgrade ($(short "$from"))"
            say "      ./scripts/upgrade.sh run --resume    on to $(short "$to"); the new panel applies its database migrations"
            say "                                           to the restored database when it starts"
            ;;
        started | rolled-back)
            # the version from before the upgrade is the one that is deployed
            if [ "$status" = rolled-back ]; then
                say "  The last upgrade was rolled back: the deployed version is the one from before it ($(short "$from"))."
            else
                say "  The last run ended before it switched anything: the deployed version still is $(short "$from")."
            fi
            if [ "$head" = "$from" ]; then
                say "  The source tree is at that version. Next: docker compose up -d"
            else
                say "  The source tree is at another version ($(short "$head")$([ -z "$branch" ] || printf ', branch %s' "$branch")), and its compose file does not fit the"
                say "  images of the deployed one: do NOT run 'docker compose up -d' in this checkout. Next, start the stack"
                say "  with the compose file of the deployed version:"
                say "      cd $top && git checkout --detach $from && cd deploy && docker compose up -d"
                say "  (In that checkout scripts/upgrade.sh is gone if that version did not have it; 'git checkout ${branch:-<branch>}'"
                say "  brings it back, and 'docker compose up -d' is then again the wrong command.)"
            fi
            ;;
        *)
            say "  Next: './scripts/upgrade.sh status' (the state '$status' in $STATE_FILE is not one this script writes)."
            ;;
    esac
}

cmd_restore_db() {
    preflight
    [ "$YES" != 1 ] || refuse_while_another_runs
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
        refuse "nothing was changed. To do it: ${LOOSE_REPAIR:+$LOOSE_REPAIR && }./scripts/upgrade.sh restore-db $file --yes"
    fi

    # From here on the database changes, and a closed terminal no longer ends the command.
    begin_log
    hold_on
    step "Loading the dump into a new database"
    kept=$(docker exec -i "$pg" sh -c "$RESTORE_SCRIPT" sh "$suffix" "$mode" <"$file" 2>"$TMP/restore.err") || {
        show_file "$TMP/restore.err" 20
        die "the restore failed (see above)"
    }
    kept=$(tail -n 1 <<<"$kept")
    step "Done"
    say "  \"$db\" now holds the dump. The database as it was is kept as \"$kept\"; when all is well, remove it:"
    say "      docker compose exec postgres sh -c 'dropdb -U \"\$POSTGRES_USER\" $kept'"
    restore_next_steps
}

main() {
    local command="${1:-}"
    LOG_ARGS=$*
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
            --archive-other-games) ARCHIVE_OTHER=1 ;;
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
    # Before anything else: can others change what root runs here? Commands that change things
    # refuse, the others warn.
    case "$command" in
        status | dry-run) check_modes warn ;;
        run | rollback) if [ "$DRY" = 1 ]; then check_modes warn; else check_modes refuse; fi ;;
        restore-db) if [ "$YES" = 1 ]; then check_modes refuse; else check_modes warn; fi ;;
    esac
    # What these three print goes into a log as well, once they begin to change things (begin_log).
    case "$command" in
        run | rollback) [ "$DRY" = 1 ] || LOG_WANTED=1 ;;
        restore-db) [ "$YES" != 1 ] || LOG_WANTED=1 ;;
    esac
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
