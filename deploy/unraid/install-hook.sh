#!/bin/bash
# Install, check or remove the MCPanel shutdown hook on Unraid.
#
#   bash install-hook.sh install   copy the hook into place (run as root)
#   bash install-hook.sh status    show whether it is installed and up to date
#   bash install-hook.sh remove    take it out again (run as root)
#
# The hook is the file "stopping_docker" next to this script. Unraid runs it from
#   /usr/local/emhttp/plugins/mcpanel/event/stopping_docker
# right before it stops Docker. That folder is in RAM and is rebuilt at every boot, so
# "install" has to run at every boot: from a User Scripts entry scheduled "At Startup of
# Array" or from a line in /boot/config/go. docs/UNRAID-SHUTDOWN.md has both.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${HERE}/stopping_docker"
# MCPANEL_HOOK_ROOT is for trying this script out somewhere else; leave it unset on Unraid.
PLUGINS="${MCPANEL_HOOK_ROOT:-/usr/local/emhttp/plugins}"
PLUGIN_DIR="${PLUGINS}/mcpanel"
EVENT_DIR="${PLUGIN_DIR}/event"
TARGET="${EVENT_DIR}/stopping_docker"

say() {
    printf '%s\n' "$*"
}

fail() {
    printf 'install-hook: %s\n' "$*" >&2
    exit 1
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

cmd_install() {
    need_source
    need_unraid
    need_root
    mkdir -p "${EVENT_DIR}" || fail "could not create ${EVENT_DIR}"
    cp "${SOURCE}" "${TARGET}.new" || fail "could not copy the hook to ${EVENT_DIR}"
    chmod 755 "${TARGET}.new" || fail "could not make the hook executable"
    mv -f "${TARGET}.new" "${TARGET}" || fail "could not put the hook in place"
    logger -t mcpanel -- "shutdown hook installed at ${TARGET}" 2>/dev/null || true
    say "Installed: ${TARGET}"
    say "Unraid runs it right before it stops Docker. It is gone after a reboot until this runs again."
}

cmd_status() {
    need_source
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
    if ! cmp -s "${SOURCE}" "${TARGET}"; then
        say "Installed, but it differs from ${SOURCE} (an older version)."
        say "Update it with: bash ${HERE}/install-hook.sh install"
        return 1
    fi
    say "Installed and up to date: ${TARGET}"
    if [ ! -x /usr/local/sbin/emhttp_event ]; then
        say "Note: /usr/local/sbin/emhttp_event was not found, so nothing here runs the hook."
    fi
    return 0
}

cmd_remove() {
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

case "${1:-}" in
    install) cmd_install ;;
    status) cmd_status ;;
    remove) cmd_remove ;;
    *)
        say "usage: bash install-hook.sh install | status | remove"
        exit 64
        ;;
esac
