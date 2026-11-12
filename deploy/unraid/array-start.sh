#!/bin/bash
# Everything MCPanel needs at an Unraid array start, in one script.
#
#   bash array-start.sh [run] [--compose-bin PATH] [--project NAME] [--wait SECONDS]
#   bash array-start.sh status     what is in place now; changes nothing
#   bash array-start.sh dry-run    what `run` would do now, command by command; changes nothing
#
# In User Scripts (Settings -> User Scripts): one script with this one line, scheduled "At Startup of Array":
#   bash /mnt/user/appdata/mcpanel-src/deploy/unraid/array-start.sh
# With the MCPanel plugin installed (deploy/unraid/plugin, docs/UNRAID.md) no User Script is needed: the
# plugin runs `array-start.sh run` at every Docker start. Two runs at the same time wait for each other.
#
# ***************************************************************************************************
# NOT RUN ON A REAL UNRAID HOST YET. It is tested against stand-ins for docker, id and logger. Before
# you rely on it: run `bash array-start.sh dry-run` in the Unraid terminal and read what it would do,
# then `bash array-start.sh run` once (it is safe to run at any time), and after the next reboot
# `bash array-start.sh status`: every line should say "ok". The User Scripts log and the syslog (tag
# mcpanel) show what each boot's run did.
# ***************************************************************************************************
#
# What `run` does, in this order (each step says what it did; a step that cannot be done is reported
# and the next one runs anyway):
#   1. Docker Compose. Unraid ships none. When `docker compose` does not answer and a standalone
#      binary is configured, it links that binary to /usr/local/lib/docker/cli-plugins/docker-compose
#      (that folder is in RAM: gone after every boot). Configure it once with --compose-bin PATH; the
#      path is remembered in /boot/config/plugins/mcpanel/array-start.env, so later boots need no
#      argument. The binary and its folder must not be writable by group or others (root runs it).
#   2. Waits for the Docker daemon, up to --wait seconds (default 300).
#   3. Installs the shutdown hook: install-hook.sh install (with --project when given or remembered).
#   4. Puts the game servers' firewall back, but only where it was installed: instance-firewall.sh
#      records INSTALLED=yes in deploy/instance-firewall.env at `install` (INSTALLED=no at `remove`).
#   5. Checks the stack. When containers of the Compose project exist, it waits up to 2 minutes until
#      panel, agent and postgres run with their networks. A container that has no network at all
#      although its NetworkMode names one (Docker 29 can drop the attachment after a failed start at
#      boot; the panel then restarts for ever with "failed to resolve host postgres") is stopped,
#      connected to that network with its service name as alias, and started again; the agent also
#      to the shared instance network. When the agent runs it repairs the containers of its project
#      itself within about a minute (AGENT_REPAIR_PLATFORM_NETWORKS, agent/mcagent/platformnet.py;
#      docs/SECURITY.md): this is the fallback for when the agent does not run (its own container broken,
#      or an agent without that repair).
# Exit status: 0 when everything is in place, 1 when something needs you (the last lines say what).
#
# Root runs this script. When group or others can write it, its folder or its state file, `run` as
# root in the MCPanel checkout tightens the checkout (chmod -R go-w) and says so; otherwise it refuses
# and prints the repair. `status` and `dry-run` only warn.
#
# MCPANEL_BOOT_STATE, MCPANEL_CLI_PLUGINS, MCPANEL_STACK_WAIT, MCPANEL_RUN_LOCK (and MCPANEL_HOOK_ROOT,
# MCPANEL_DISK_CFG for install-hook.sh) are for trying this script out somewhere else; leave them unset on
# Unraid.
set -u
umask 022
exec 3>&1 # the dry run's "would run" lines, also from commands whose output is discarded

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="${HERE}/$(basename "${BASH_SOURCE[0]}")"
DEPLOY="$(cd "${HERE}/.." && pwd)"
STATE_DIR="${MCPANEL_BOOT_STATE:-/boot/config/plugins/mcpanel}"
STATE_FILE="${STATE_DIR}/array-start.env"
CLI_DIR="${MCPANEL_CLI_PLUGINS:-/usr/local/lib/docker/cli-plugins}"
# one `run` at a time (a User Script and the plugin may both start one at boot); /run is root's, in RAM
RUN_LOCK="${MCPANEL_RUN_LOCK:-/run/mcpanel-array-start.lock}"
PLUGIN_EVENT="${MCPANEL_HOOK_ROOT:-/usr/local/emhttp/plugins}/mcpanel/event/docker_started"
STACK_WAIT="${MCPANEL_STACK_WAIT:-120}"
FIREWALL_STATE="${DEPLOY}/instance-firewall.env"
PLATFORM="panel agent postgres"

WAIT=300
PROJECT=""
COMPOSE_BIN=""
NEW_COMPOSE_BIN=""
NEW_PROJECT=""
DRY=0
PROBLEMS=()

say() { printf '%s\n' "$*"; }
note() { # note MESSAGE: to the output and, in `run`, to the syslog
    say "$*"
    [ "${DRY}" = 1 ] || logger -t mcpanel -- "array-start: $*" 2>/dev/null || true
}
problem() { # problem MESSAGE: something the owner must do; the run goes on
    PROBLEMS+=("$*")
    note "NEEDS YOU: $*"
}
fail() {
    printf 'array-start: %s\n' "$*" >&2
    logger -t mcpanel -- "array-start: $*" 2>/dev/null || true
    exit 1
}
usage() {
    say "usage: bash array-start.sh [run|status|dry-run] [--compose-bin PATH] [--project NAME] [--wait SECONDS]"
    exit 64
}
act() { # act COMMAND...: run it, or in a dry run say it
    if [ "${DRY}" = 1 ]; then
        say "  would run: $*" >&3
        return 0
    fi
    "$@"
}

# --- who can change what root runs (the rule of the other scripts of this folder) -------------------
loose_modes() {
    local -a paths=("${SELF}" "${HERE}")
    [ ! -e "${STATE_FILE}" ] || paths+=("${STATE_FILE}")
    find -L "${paths[@]}" -maxdepth 0 -perm /022 -printf '%m %p\n' 2>/dev/null
    return 0
}

check_modes() { # check_modes refuse|warn
    local found top real repair line nl
    found="$(loose_modes)"
    [ -n "${found}" ] || return 0
    nl=$'\n'
    top="$(cd "${HERE}/../.." 2>/dev/null && pwd)"
    repair="chmod go-w"
    if [ -n "${top}" ] && [ "${top}/deploy/unraid" -ef "${HERE}" ]; then
        printf -v repair 'chmod -R go-w %q' "${top}"
        while read -r _ line; do
            [[ "${line}" == "${top}"/* ]] || printf -v repair '%s && chmod go-w %q' "${repair}" "${line}"
        done <<<"${found}"
        if [ "$1" = refuse ] && [ "$(id -u)" -eq 0 ] && real="$(cd -P "${top}" 2>/dev/null && pwd -P)" &&
            chmod -R go-w -- "${real}" 2>/dev/null && [ -z "$(loose_modes)" ]; then
            note "permissions of ${top} were loose and have been tightened (chmod -R go-w; were: ${found//${nl}/, }); if you did not expect this, check 'git status' there"
            return 0
        fi
    else
        while read -r _ line; do printf -v repair '%s %q' "${repair}" "${line}"; done <<<"${found}"
    fi
    if [ "$1" = refuse ]; then
        fail "refusing: group or others can write to what root runs here (mode, path): ${found//${nl}/, }. Nothing was done. Repair it (as root), then run this again: ${repair}"
    fi
    say "WARNING: group or others can write to what root runs here (mode, path): ${found//${nl}/, }"
    say "         'run' refuses until it is repaired (as root, in a checkout 'run' does it itself): ${repair}"
}

# --- the remembered settings ------------------------------------------------------------------------
read_state() {
    local line value
    [ -f "${STATE_FILE}" ] || return 0
    while IFS= read -r line || [ -n "${line}" ]; do
        value="${line#*=}"
        case "${line}" in
            COMPOSE_BIN=/*) [ -n "${NEW_COMPOSE_BIN}" ] || COMPOSE_BIN="${value}" ;;
            PROJECT=*) [ -n "${NEW_PROJECT}" ] || PROJECT="${value}" ;;
        esac
    done <"${STATE_FILE}"
    [ -z "${PROJECT}" ] || [[ "${PROJECT}" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || PROJECT=""
}

save_state() {
    local text
    text="$(
        say "# deploy/unraid/array-start.sh: what every boot's run uses (--compose-bin / --project change it)"
        [ -z "${COMPOSE_BIN}" ] || say "COMPOSE_BIN=${COMPOSE_BIN}"
        [ -z "${PROJECT}" ] || say "PROJECT=${PROJECT}"
    )"
    # the same as before: no write (the plugin passes its settings at every boot; the flash drive is written rarely)
    [ "$(cat "${STATE_FILE}" 2>/dev/null)" != "${text}" ] || return 0
    (
        umask 077
        mkdir -p "${STATE_DIR}" || exit 1
        say "${text}" >"${STATE_FILE}.new" && mv -f "${STATE_FILE}.new" "${STATE_FILE}"
    ) || problem "could not write ${STATE_FILE}: --compose-bin/--project are not remembered for the next boot"
}

# one_at_a_time: wait (up to 15 minutes) while another `run` is at work; without flock, go on
one_at_a_time() {
    command -v flock >/dev/null 2>&1 || return 0
    { exec 8>>"${RUN_LOCK}"; } 2>/dev/null || return 0
    flock -n 8 && return 0
    note "another array-start.sh run is at work: waiting for it (up to 15 minutes)"
    flock -w 900 8 || fail "another array-start.sh run did not end within 15 minutes: nothing was done"
}

# --- 1. Docker Compose --------------------------------------------------------------------------------
compose_answers() { docker compose version >/dev/null 2>&1; }

compose_step() {
    local loose
    if compose_answers; then
        note "Compose: ok ($(docker compose version 2>/dev/null | head -n 1))"
        return 0
    fi
    if [ -z "${COMPOSE_BIN}" ]; then
        problem "Docker Compose is missing (Unraid ships none) and no binary is configured. Download the standalone binary (docker-compose-linux-x86_64 from https://github.com/docker/compose/releases), keep it on a pool or the flash drive where only root can write, make it executable, then run once: bash ${SELF} --compose-bin /path/to/docker-compose"
        return 1
    fi
    if [ ! -f "${COMPOSE_BIN}" ] || [ ! -x "${COMPOSE_BIN}" ]; then
        problem "the Compose binary ${COMPOSE_BIN} does not exist or is not executable (is its pool mounted?); fix it or name another with --compose-bin"
        return 1
    fi
    loose="$(find -L "${COMPOSE_BIN}" "$(dirname "${COMPOSE_BIN}")" -maxdepth 0 -perm /022 -printf '%m %p, ' 2>/dev/null)"
    if [ -n "${loose}" ]; then
        problem "not linked: group or others can write ${loose% , }and root would run it. Repair: chmod go-w ${COMPOSE_BIN} $(dirname "${COMPOSE_BIN}")"
        return 1
    fi
    [ "${DRY}" = 0 ] || say "Compose: 'docker compose' does not answer; 'run' links ${COMPOSE_BIN}:"
    act mkdir -p "${CLI_DIR}" || {
        problem "could not create ${CLI_DIR}"
        return 1
    }
    act ln -sfn "${COMPOSE_BIN}" "${CLI_DIR}/docker-compose" || {
        problem "could not link ${COMPOSE_BIN} to ${CLI_DIR}/docker-compose"
        return 1
    }
    if [ "${DRY}" = 1 ]; then
        say "  (then 'docker compose version' should answer)"
        return 0
    fi
    if compose_answers; then
        note "Compose: linked ${COMPOSE_BIN} to ${CLI_DIR}/docker-compose ($(docker compose version 2>/dev/null | head -n 1))"
        return 0
    fi
    problem "linked ${COMPOSE_BIN} to ${CLI_DIR}/docker-compose, but 'docker compose version' still fails: is it the Compose binary for this machine?"
    return 1
}

# --- 2. the Docker daemon -------------------------------------------------------------------------------
docker_answers() { docker version --format '{{.Server.Version}}' >/dev/null 2>&1; }

wait_docker() {
    local waited=0
    if [ "${DRY}" = 1 ]; then
        if docker_answers; then say "Docker: answers now"; else say "Docker: does not answer now (run would wait up to ${WAIT} s)"; fi
        return 0
    fi
    while ! docker_answers; do
        if [ "${waited}" -ge "${WAIT}" ]; then
            problem "the Docker daemon did not answer within ${WAIT} s: the firewall and the stack check were skipped (is Docker enabled, Settings -> Docker?)"
            return 1
        fi
        sleep 5
        waited=$((waited + 5))
    done
    note "Docker: answers$([ "${waited}" = 0 ] || printf ' after %s s' "${waited}")"
}

# --- 3. the shutdown hook -------------------------------------------------------------------------------
hook_step() {
    local out
    local -a args=(install)
    [ -z "${PROJECT}" ] || args+=(--project "${PROJECT}")
    if [ "${DRY}" = 1 ]; then
        act bash "${HERE}/install-hook.sh" "${args[@]}"
        return 0
    fi
    if out="$(bash "${HERE}/install-hook.sh" "${args[@]}" 2>&1)"; then
        note "Shutdown hook: installed ($(printf '%s\n' "${out}" | head -n 1))"
        printf '%s\n' "${out}" | grep -i 'warning' | while IFS= read -r line; do note "  ${line}"; done
        return 0
    fi
    problem "the shutdown hook was not installed: $(printf '%s\n' "${out}" | tr '\n' ' ')"
    return 1
}

# --- 4. the game servers' firewall ----------------------------------------------------------------------
firewall_remembered() { [ -f "${FIREWALL_STATE}" ] && grep -qx 'INSTALLED=yes' "${FIREWALL_STATE}"; }

firewall_step() {
    local out
    if ! firewall_remembered; then
        note "Firewall: not installed on this host by its owner (instance-firewall.sh install does it): nothing to do"
        return 0
    fi
    if [ "${DRY}" = 1 ]; then
        act bash "${HERE}/instance-firewall.sh" install --wait 120
        return 0
    fi
    if out="$(bash "${HERE}/instance-firewall.sh" install --wait 120 2>&1)"; then
        note "Firewall: installed again ($(printf '%s\n' "${out}" | grep -m 1 "^Game containers' addresses" || true))"
        return 0
    fi
    problem "the game servers' firewall could not be installed again: $(printf '%s\n' "${out}" | tail -n 3 | tr '\n' ' ')"
    return 1
}

# --- 5. the stack ---------------------------------------------------------------------------------------
named_network() { # the NetworkMode of a Compose container names a network (not a mode of Docker's own)
    case "$1" in '' | default | bridge | host | none | container:*) return 1 ;; esac
    return 0
}

# Lines "<id>|<service>|<running>|<network mode>|<number of networks>|<name>" of the platform containers
platform_containers() {
    local id line service
    for id in $(docker ps -a -q --filter "label=com.docker.compose.project=${PROJECT:-mcpanel}" 2>/dev/null); do
        line="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}|{{.State.Running}}|{{.HostConfig.NetworkMode}}|{{len .NetworkSettings.Networks}}|{{.Name}}' "${id}" 2>/dev/null)" || continue
        service="${line%%|*}"
        case " ${PLATFORM} " in *" ${service} "*) printf '%s|%s\n' "${id}" "${line}" ;; esac
    done
}

instance_network() { # the shared instance network's name (deploy/.env, else mcpanel_instances)
    local value=""
    [ ! -r "${DEPLOY}/.env" ] || value="$(sed -n 's/^AGENT_INSTANCE_NETWORK=\([A-Za-z0-9_.-]*\)\r\{0,1\}$/\1/p' "${DEPLOY}/.env" | tail -n 1)"
    printf '%s\n' "${value:-mcpanel_instances}"
}

repair() { # repair ID SERVICE MODE NAME
    local id="$1" service="$2" mode="$3" name="${4#/}" shared
    note "Stack: ${name} has no network although it belongs on ${mode} (Docker lost the attachment at a start): stop, connect it with the alias ${service}, start"
    act docker stop -t 30 "${id}" >/dev/null 2>&1
    if ! act docker network connect --alias "${service}" "${mode}" "${id}"; then
        problem "could not connect ${name} to ${mode}: in deploy/, 'docker compose up -d --force-recreate ${service}' makes the container again"
        return 1
    fi
    if [ "${service}" = agent ]; then
        shared="$(instance_network)"
        if docker network inspect "${shared}" >/dev/null 2>&1; then
            act docker network connect "${shared}" "${id}" || note "  could not connect ${name} to ${shared} as well"
        fi
    fi
    if ! act docker start "${id}" >/dev/null; then
        problem "connected ${name} to ${mode}, but it did not start: 'docker logs ${name}' says why"
        return 1
    fi
    [ "${DRY}" = 1 ] || note "Stack: ${name} repaired and started"
}

stack_step() {
    local waited=0 rows id service running mode nets name pending broken
    rows="$(platform_containers)"
    if [ -z "${rows}" ]; then
        note "Stack: no container of the Compose project '${PROJECT:-mcpanel}' (not started yet, or another --project): nothing to check"
        return 0
    fi
    while :; do
        pending=0
        broken=""
        while IFS='|' read -r id service running mode nets name; do
            [ -n "${id}" ] || continue
            if [ "${nets}" = 0 ] && named_network "${mode}"; then
                broken="${broken}${id}|${service}|${running}|${mode}|${nets}|${name}"$'\n'
            elif [ "${running}" != true ]; then
                pending=1
            fi
        done <<<"${rows}"
        if [ -z "${broken}" ] && [ "${pending}" = 0 ]; then
            note "Stack: panel, agent and postgres run with their networks"
            return 0
        fi
        if [ "${DRY}" = 1 ] || [ "${waited}" -ge "${STACK_WAIT}" ]; then break; fi
        sleep 5
        waited=$((waited + 5))
        rows="$(platform_containers)"
    done
    local status=0
    while IFS='|' read -r id service running mode nets name; do
        [ -n "${id}" ] || continue
        repair "${id}" "${service}" "${mode}" "${name}" || status=1
    done <<<"${broken}"
    [ "${DRY}" = 1 ] && return 0
    while IFS='|' read -r id service running mode nets name; do
        [ -n "${id}" ] || continue
        [ "${running}" = true ] || [ "${nets}" = 0 ] ||
            problem "${name#/} is not running after ${STACK_WAIT} s (with its network): 'docker logs ${name#/}' says why"
    done <<<"$(platform_containers)"
    return "${status}"
}

# --- commands -------------------------------------------------------------------------------------------
summary() {
    local entry
    say ""
    if [ "${#PROBLEMS[@]}" = 0 ]; then
        note "Done: everything is in place."
        return 0
    fi
    note "Done, but ${#PROBLEMS[@]} thing(s) need you:"
    for entry in "${PROBLEMS[@]}"; do say "  - ${entry}"; done
    return 1
}

cmd_run() {
    [ "$(id -u)" -eq 0 ] || fail "run this as root (the Unraid terminal and User Scripts are root)"
    check_modes refuse
    one_at_a_time
    read_state
    if [ -n "${NEW_COMPOSE_BIN}" ]; then
        if [ -f "${NEW_COMPOSE_BIN}" ] && [ -x "${NEW_COMPOSE_BIN}" ]; then
            COMPOSE_BIN="${NEW_COMPOSE_BIN}"
        else
            problem "--compose-bin ${NEW_COMPOSE_BIN}: no executable file there; not remembered"
            NEW_COMPOSE_BIN=""
        fi
    fi
    [ -z "${NEW_PROJECT}" ] || PROJECT="${NEW_PROJECT}"
    [ -z "${NEW_COMPOSE_BIN}${NEW_PROJECT}" ] || save_state
    compose_step
    if wait_docker; then
        hook_step
        firewall_step
        stack_step
    else
        hook_step # needs no Docker
    fi
    summary
}

cmd_dry_run() {
    DRY=1
    check_modes warn
    read_state
    [ -z "${NEW_COMPOSE_BIN}" ] || COMPOSE_BIN="${NEW_COMPOSE_BIN}"
    [ -z "${NEW_PROJECT}" ] || PROJECT="${NEW_PROJECT}"
    say "array-start.sh dry-run: what 'run' would do now (nothing is changed)"
    [ -z "${NEW_COMPOSE_BIN}${NEW_PROJECT}" ] || say "  would remember in ${STATE_FILE}: ${COMPOSE_BIN:+COMPOSE_BIN=${COMPOSE_BIN} }${PROJECT:+PROJECT=${PROJECT}}"
    compose_step
    wait_docker
    hook_step
    firewall_step
    if docker_answers; then stack_step; else say "Stack: not checked (Docker does not answer)"; fi
    say ""
    say "Nothing was changed.$([ "${#PROBLEMS[@]}" = 0 ] || printf ' %s thing(s) need you first (NEEDS YOU above).' "${#PROBLEMS[@]}")"
    [ "${#PROBLEMS[@]}" = 0 ]
}

cmd_status() {
    local out ok=1 rows id service running mode nets name given_bin="${NEW_COMPOSE_BIN}" given_project="${NEW_PROJECT}"
    DRY=1
    check_modes warn
    NEW_COMPOSE_BIN="" NEW_PROJECT=""
    read_state
    say "array-start.sh status (changes nothing)"
    say "  remembered (${STATE_FILE}): ${COMPOSE_BIN:+COMPOSE_BIN=${COMPOSE_BIN} }${PROJECT:+PROJECT=${PROJECT}}$([ -n "${COMPOSE_BIN}${PROJECT}" ] || printf 'nothing')"
    # what a `run` with the same arguments would use (the plugin passes its settings)
    [ -z "${given_bin}" ] || COMPOSE_BIN="${given_bin}"
    [ -z "${given_project}" ] || PROJECT="${given_project}"
    [ -z "${given_bin}${given_project}" ] || say "  given: ${given_bin:+--compose-bin ${given_bin} }${given_project:+--project ${given_project}}"
    if compose_answers; then
        say "  ok    Compose: $(docker compose version 2>/dev/null | head -n 1)"
    else
        ok=0
        say "  MISSING Compose: 'docker compose' does not answer$([ -n "${COMPOSE_BIN}" ] && printf ' (run links %s)' "${COMPOSE_BIN}" || printf ' (configure a binary: --compose-bin PATH)')"
    fi
    if docker_answers; then say "  ok    Docker answers"; else
        ok=0
        say "  DOWN  Docker does not answer"
    fi
    if out="$(bash "${HERE}/install-hook.sh" status 2>&1)"; then
        say "  ok    Shutdown hook: $(printf '%s\n' "${out}" | head -n 1)"
    else
        ok=0
        say "  MISSING Shutdown hook: $(printf '%s\n' "${out}" | head -n 1)"
    fi
    if firewall_remembered; then
        out="$(bash "${HERE}/instance-firewall.sh" status 2>&1)"
        case "${out}" in
            *"installed and up to date"*) say "  ok    Firewall: installed and up to date" ;;
            *)
                ok=0
                say "  CHECK Firewall: $(printf '%s\n' "${out}" | grep '^Instance firewall' | tail -n 1)"
                ;;
        esac
    else
        say "  ok    Firewall: not installed by the owner (nothing to put back at boot)"
    fi
    rows="$(platform_containers)"
    if [ -z "${rows}" ]; then
        say "  --    Stack: no container of the Compose project '${PROJECT:-mcpanel}'"
    fi
    while IFS='|' read -r id service running mode nets name; do
        [ -n "${id}" ] || continue
        if [ "${nets}" = 0 ] && named_network "${mode}"; then
            ok=0
            say "  REPAIR ${name#/}: no network although it belongs on ${mode} ('run' repairs it)"
        elif [ "${running}" != true ]; then
            ok=0
            say "  DOWN  ${name#/}: not running"
        else
            say "  ok    ${name#/}: running, ${nets} network(s)"
        fi
    done <<<"${rows}"
    if [ -x "${PLUGIN_EVENT}" ]; then
        say "  The MCPanel plugin runs 'array-start.sh run' at every Docker start: no User Script is needed for it."
    else
        say "  User Scripts line (At Startup of Array): bash ${SELF}"
    fi
    [ "${ok}" = 1 ]
}

action="run"
case "${1:-}" in
    run | status | dry-run)
        action="$1"
        shift
        ;;
esac
while [ "$#" -gt 0 ]; do
    case "$1" in
        --compose-bin)
            [ "$#" -ge 2 ] || fail "--compose-bin needs the path of the docker-compose binary"
            [[ "$2" == /* ]] || fail "--compose-bin needs an absolute path (got '$2')"
            [[ "$2" != *$'\n'* ]] || fail "--compose-bin: not a path"
            NEW_COMPOSE_BIN="$2"
            shift 2
            ;;
        --project)
            [ "$#" -ge 2 ] || fail "--project needs a name"
            [[ "$2" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || fail "not a Compose project name: '$2' (see 'docker compose ls')"
            NEW_PROJECT="$2"
            shift 2
            ;;
        --wait)
            [ "$#" -ge 2 ] || fail "--wait needs a number of seconds"
            [[ "$2" =~ ^[0-9]{1,5}$ ]] || fail "--wait needs a number of seconds"
            WAIT=$((10#$2))
            shift 2
            ;;
        *) usage ;;
    esac
done
case "${action}" in
    run) cmd_run ;;
    status) cmd_status ;;
    dry-run) cmd_dry_run ;;
esac
