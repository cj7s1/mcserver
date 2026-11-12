#!/bin/bash
# Install, check or remove the MCPanel shutdown hook on Unraid.
#
#   bash install-hook.sh install [--project <name>]   copy the hook into place (run as root)
#   bash install-hook.sh status                       show whether it is installed and up to date
#   bash install-hook.sh remove                       take it out again (run as root)
#
# The hook is the file "stopping_docker" next to this script. Unraid runs it from
#   /usr/local/emhttp/plugins/mcpanel/event/stopping_docker
# right before it stops Docker. That folder is in RAM and is rebuilt at every boot, so
# "install" has to run at every boot: from a User Scripts entry scheduled "At Startup of
# Array" or from a line in /boot/config/go. docs/UNRAID-SHUTDOWN.md has both.
#
# --project <name>: the Compose project of the panel stack, if it is not "mcpanel" (the hook
# finds the panel container by it; see "docker compose ls"). Unraid passes no environment to
# the hook, so the name is written into the installed copy. Give it every time "install" runs.
#
# "install" and "status" also read Unraid's shutdown time-out and warn when it is too short
# for the hook (see check_shutdown_timeout).
#
# Root runs this script at every array start and the hook it installs at shutdown. Whoever can
# write this script, the hook file or their folder (an SMB user, a container that mounts the
# folder) could run code as root then. When group or others can write one of them, "install" and
# "remove" run as root tighten the MCPanel checkout themselves (chmod -R go-w), say so on stderr
# and in the syslog, and go on: a refusal at an unattended array start would leave the server
# without its shutdown hook. Not as root, outside a checkout, or when that does not help, they
# refuse and print the repair; "status" only warns (see check_modes).
set -u
# The folders "install" makes: never writable by group or others, whatever the umask of the
# shell or of User Scripts that runs this.
umask 022

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="${HERE}/$(basename "${BASH_SOURCE[0]}")"
SOURCE="${HERE}/stopping_docker"
# MCPANEL_HOOK_ROOT and MCPANEL_DISK_CFG are for trying this script out somewhere else; leave
# them unset on Unraid.
PLUGINS="${MCPANEL_HOOK_ROOT:-/usr/local/emhttp/plugins}"
PLUGIN_DIR="${PLUGINS}/mcpanel"
EVENT_DIR="${PLUGIN_DIR}/event"
TARGET="${EVENT_DIR}/stopping_docker"
DISK_CFG="${MCPANEL_DISK_CFG:-/boot/config/disk.cfg}"
DEFAULT_PROJECT="mcpanel"
# The hook may use 60 s (70 s with its watchdog) before Unraid starts to stop Docker and the
# array, which need the time Unraid's default of 90 s was meant for.
NEEDED_TIMEOUT=150

say() {
    printf '%s\n' "$*"
}

fail() {
    printf 'install-hook: %s\n' "$*" >&2
    exit 1
}

usage() {
    say "usage: bash install-hook.sh install [--project <name>] | status | remove"
    exit 64
}

need_source() {
    [ -f "${SOURCE}" ] || fail "the hook file is missing: ${SOURCE}"
    bash -n "${SOURCE}" || fail "the hook file has a syntax error: ${SOURCE}"
}

need_unraid() {
    [ -d "${PLUGINS}" ] || fail "${PLUGINS} does not exist: this does not look like an Unraid server"
}

need_root() {
    [ -n "${MCPANEL_HOOK_ROOT:-}" ] || [ "$(id -u)" -eq 0 ] || fail "run this as root (the Unraid terminal is root)"
}

# loose_modes ABSOLUTE-PATH...: lines "<mode> <path>" of those that group or others can write
# (symbolic links followed).
loose_modes() {
    find -L "$@" -maxdepth 0 -perm /022 -printf '%m %p\n' 2>/dev/null
    return 0
}

# check_modes refuse|warn: this script, the hook file and their folder must not be writable by
# group or others. In an MCPanel checkout the repair is a chmod of the whole checkout (a `git pull`
# under a loose umask rewrites more files than these); elsewhere (a copy in /boot/config/mcpanel)
# of the paths found. "install" and "remove" (refuse) run as root do the checkout's repair
# themselves and go on when it worked; a path outside the checkout is never changed.
check_modes() {
    local found top real repair line nl how="'install' and 'remove' refuse until it is repaired (as root):"
    found="$(loose_modes "${SELF}" "${SOURCE}" "${HERE}")"
    [ -n "${found}" ] || return 0
    nl=$'\n'
    top="$(cd "${HERE}/../.." 2>/dev/null && pwd)"
    if [ -n "${top}" ] && [ "${top}/deploy/unraid" -ef "${HERE}" ]; then
        printf -v repair 'chmod -R go-w %q' "${top}"
        how="'install' and 'remove' run as root repair it themselves; or now (as root):"
        # Its real path: no symbolic link on the command line (chmod -R does not follow the links
        # it meets inside), so nothing outside the checkout is changed.
        if [ "$1" = refuse ] && [ "$(id -u)" -eq 0 ] && real="$(cd -P "${top}" 2>/dev/null && pwd -P)" &&
            chmod -R go-w -- "${real}" 2>/dev/null && [ -z "$(loose_modes "${SELF}" "${SOURCE}" "${HERE}")" ]; then
            line="permissions of ${top} were loose and have been tightened (chmod -R go-w; were: ${found//${nl}/, }); if you did not expect this, check 'git status' there"
            printf 'install-hook: WARNING: %s\n' "${line}" >&2
            logger -t mcpanel -- "shutdown hook: ${line}" 2>/dev/null || true
            return 0
        fi
    else
        repair="chmod go-w"
        while read -r _ line; do printf -v repair '%s %q' "${repair}" "${line}"; done <<<"${found}"
    fi
    found="    ${found//${nl}/${nl}    }"
    if [ "$1" = refuse ]; then
        logger -t mcpanel -- "shutdown hook NOT installed or removed: group or others can write to ${HERE} (repair as root: ${repair})" 2>/dev/null || true
        fail "refusing: group or others can write to what root runs here (mode, path):
${found}
Whoever can change these files (an SMB user, a container that mounts the folder) could run code
as root at the next array start or shutdown. The hook was neither installed nor removed. Repair it
(as root), then run this command again:
    ${repair}"
    fi
    say "WARNING: group or others can write to what root runs here (mode, path):"
    say "${found}"
    say "         ${how}"
    say "    ${repair}"
}

# The hook as it is installed for a Compose project: the source with that name as the default
# of its PROJECT line.
render() {
    sed "s/^PROJECT=\"\\\${MCPANEL_COMPOSE_PROJECT:-[^}]*}\"/PROJECT=\"\\\${MCPANEL_COMPOSE_PROJECT:-$1}\"/" "${SOURCE}"
}

# The Compose project an installed hook looks for (empty if its PROJECT line is not there).
project_of() {
    sed -n 's/^PROJECT="\${MCPANEL_COMPOSE_PROJECT:-\([^}]*\)}".*$/\1/p' "$1" | head -n 1
}

# Unraid waits this long (Settings > Disk Settings > Shutdown time-out) for a clean shutdown
# and forces one after that; an unclean shutdown starts a parity check. The hook runs inside
# that time. Prints a warning when the value is below NEEDED_TIMEOUT; never fails.
check_shutdown_timeout() {
    local value
    if [ ! -r "${DISK_CFG}" ]; then
        say "Note: Unraid's shutdown time-out could not be read (${DISK_CFG} is missing)."
        say "      Check Settings > Disk Settings > Shutdown time-out: it should be at least ${NEEDED_TIMEOUT} seconds."
        return 0
    fi
    value="$(tr -d '\r' <"${DISK_CFG}" | sed -n 's/^shutdownTimeout="\{0,1\}\([0-9]\{1,6\}\)"\{0,1\}[[:space:]]*$/\1/p' | tail -n 1)"
    if [ -z "${value}" ]; then
        say "WARNING: no shutdown time-out is set in ${DISK_CFG}, so Unraid uses its default of 90 seconds."
    elif [ "${value}" -ge "${NEEDED_TIMEOUT}" ]; then
        say "Unraid's shutdown time-out is ${value} seconds: enough for the hook."
        return 0
    else
        say "WARNING: Unraid's shutdown time-out is ${value} seconds."
    fi
    say "         The hook can take 60 to 70 seconds before Unraid starts to stop Docker and the array."
    say "         What is left may not be enough: Unraid then forces the shutdown (an unclean shutdown,"
    say "         followed by a parity check at the next start)."
    say "         Set Settings > Disk Settings > Shutdown time-out to at least ${NEEDED_TIMEOUT}."
    logger -t mcpanel -- "shutdown hook: Unraid's shutdown time-out is ${value:-90 (default)} s; set it to at least ${NEEDED_TIMEOUT} s (Settings > Disk Settings)" 2>/dev/null || true
    return 0
}

cmd_install() {
    local project="${DEFAULT_PROJECT}"
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --project)
                [ "$#" -ge 2 ] || fail "--project needs a name"
                project="$2"
                shift 2
                ;;
            --project=*)
                project="${1#--project=}"
                shift
                ;;
            *) usage ;;
        esac
    done
    # what Docker Compose accepts as a project name; nothing else may end up in the hook
    [[ "${project}" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] ||
        fail "not a Compose project name: '${project}' (lowercase letters, digits, - and _; see 'docker compose ls')"
    need_source
    check_modes refuse
    need_unraid
    need_root
    mkdir -p "${EVENT_DIR}" || fail "could not create ${EVENT_DIR}"
    render "${project}" >"${TARGET}.new" || fail "could not copy the hook to ${EVENT_DIR}"
    if [ "$(project_of "${TARGET}.new")" != "${project}" ] || ! bash -n "${TARGET}.new"; then
        rm -f "${TARGET}.new"
        fail "could not write the project name into the hook (its PROJECT line was not found)"
    fi
    chmod 755 "${TARGET}.new" || fail "could not make the hook executable"
    mv -f "${TARGET}.new" "${TARGET}" || fail "could not put the hook in place"
    logger -t mcpanel -- "shutdown hook installed at ${TARGET} (Compose project ${project})" 2>/dev/null || true
    say "Installed: ${TARGET}"
    say "It looks for the panel container of the Compose project '${project}'."
    say "Unraid runs it right before it stops Docker. It is gone after a reboot until this runs again."
    check_shutdown_timeout
}

cmd_status() {
    local project
    [ "$#" -eq 0 ] || usage
    need_source
    check_modes warn
    if [ ! -e "${TARGET}" ]; then
        say "Not installed: ${TARGET} does not exist."
        say "Install it with: bash ${HERE}/install-hook.sh install"
        return 1
    fi
    if [ ! -x "${TARGET}" ]; then
        say "Installed but not executable, so Unraid skips it: ${TARGET}"
        say "Repair it with: bash ${HERE}/install-hook.sh install"
        return 1
    fi
    project="$(project_of "${TARGET}")"
    if [ -z "${project}" ] || ! render "${project}" | cmp -s - "${TARGET}"; then
        say "Installed, but it differs from ${SOURCE} (an older version)."
        say "Update it with: bash ${HERE}/install-hook.sh install${project:+ --project ${project}}"
        return 1
    fi
    say "Installed and up to date: ${TARGET}"
    say "It looks for the panel container of the Compose project '${project}'."
    if [ ! -x /usr/local/sbin/emhttp_event ]; then
        say "Note: /usr/local/sbin/emhttp_event was not found, so nothing here runs the hook."
    fi
    check_shutdown_timeout
    return 0
}

cmd_remove() {
    [ "$#" -eq 0 ] || usage
    check_modes refuse
    need_unraid
    need_root
    if [ ! -e "${TARGET}" ]; then
        say "Nothing to remove: ${TARGET} does not exist."
        return 0
    fi
    rm -f "${TARGET}" || fail "could not remove ${TARGET}"
    # only the folders this script made, and only when they are empty
    rmdir "${EVENT_DIR}" 2>/dev/null && rmdir "${PLUGIN_DIR}" 2>/dev/null
    logger -t mcpanel -- "shutdown hook removed from ${TARGET}" 2>/dev/null || true
    say "Removed: ${TARGET}"
    say "If a User Scripts entry or /boot/config/go installs it at boot, take that out as well."
    return 0
}

action="${1:-}"
[ "$#" -gt 0 ] && shift
case "${action}" in
    install) cmd_install "$@" ;;
    status) cmd_status "$@" ;;
    remove) cmd_remove "$@" ;;
    *) usage ;;
esac
