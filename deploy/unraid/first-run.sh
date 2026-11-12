#!/bin/bash
# The first-run checklist of the MCPanel plugin for Unraid (docs/UNRAID.md, "The Unraid plugin"), as a program.
# Run it as root in Unraid's terminal; it works from the checkout or as a single file copied anywhere.
#
#   bash first-run.sh [status]          each item of the checklist: OK / TO DO / NEEDS YOU / CANNOT TELL, and the
#                                       one command to run next (the default; changes nothing)
#   bash first-run.sh dry-run           the exact commands `run` would have the plugin run (changes nothing)
#   bash first-run.sh run [--yes] [--compose-bin PATH] [--checkout PATH] [--project NAME]
#                                       steps 1 to 4 of the checklist, in order, asking before each step that
#                                       changes something (--yes: yes to all); safe to run again
#   bash first-run.sh after-reboot      step 5: did the boot tasks run at this boot, the syslog, the hook, the stack
#   bash first-run.sh firewall [--yes]  the game servers' firewall: status, dry run, install, then a self-test from
#                                       a short-lived container on the shared instance network and on one server's
#                                       own network (when one exists); removed again when the internet does not work
#   bash first-run.sh firewall test     the self-test only
#   bash first-run.sh firewall remove   take the firewall out again
#
# It does nothing of its own: every change goes through the installed plugin's runner
# (/usr/local/emhttp/plugins/mcpanel/bin/mcpanel: facts, detect, save, describe, start), so it can never do
# what the plugin's page could not; everything else it does is reading. The self-test of `firewall` runs one
# throw-away container of its own (named mcpanel-firstrun-selftest, removed afterwards).
#
# ***************************************************************************************************
# RAN ON ONE REAL UNRAID 7.3.4 SERVER ON 2026-11-12 (plugin 2026.11.12a): `status`, `dry-run`, `run`,
# `firewall` (installed, and the self-test from the shared instance network: the server's web UI and the
# router blocked, the internet reachable by address and by name) and `firewall test`. NOT SEEN THERE YET:
# `after-reboot`, `firewall remove`, the self-test from a server's own network, Install Compose. It is also
# tested against the plugin's real runner with stand-ins for docker, id, logger, curl, ip and iptables.
# Read what `status` and `dry-run` say before you use `run`.
# ***************************************************************************************************
#
# MCPANEL_PLUGIN_ROOT (a folder that stands for /) is for tests only, as for the plugin's runner.
set -u
umask 022
PATH="${PATH:+${PATH}:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

R="${MCPANEL_PLUGIN_ROOT:-}"
PLUGIN_DIR="${R}/usr/local/emhttp/plugins/mcpanel"
RUNNER="${PLUGIN_DIR}/bin/mcpanel"
OWN_COMPOSE="${R}/usr/local/lib/mcpanel/docker-compose"
SYSLOG="${R}/var/log/syslog"
IDENT="${R}/boot/config/ident.cfg"
SELFTEST=mcpanel-firstrun-selftest
SELF="bash $(basename "${BASH_SOURCE[0]}")"
# documented default path, for the hints (the checkout copy of this script)
[ ! -f /mnt/user/appdata/mcpanel-src/deploy/unraid/first-run.sh ] || SELF="bash /mnt/user/appdata/mcpanel-src/deploy/unraid/first-run.sh"

YES=0
COMPOSE_ARG=""
CHECKOUT_ARG=""
PROJECT_ARG=""
declare -A F=()   # the runner's facts
NEXT=""           # the one command to run next (status)
ISSUES=0

# --- output ------------------------------------------------------------------------------------------
if [ -t 1 ]; then
    C_OK=$'\e[32m' C_TODO=$'\e[33m' C_BAD=$'\e[31m' C_DIM=$'\e[2m' C_END=$'\e[0m'
else
    C_OK="" C_TODO="" C_BAD="" C_DIM="" C_END=""
fi
say() { printf '%s\n' "$*"; }
item() { # item STATE LABEL WHY: one line of the checklist
    local colour="${C_OK}"
    case "$1" in
        "TO DO") colour="${C_TODO}" ;;
        "NEEDS YOU" | FAIL) colour="${C_BAD}" ;;
        "CANNOT TELL") colour="${C_DIM}" ;;
    esac
    printf '%s%-11s%s %-22s %s\n' "${colour}" "$1" "${C_END}" "$2" "$3"
    case "$1" in OK | PASS) ;; *) ISSUES=$((ISSUES + 1)) ;; esac
}
fail() { # fail MESSAGE: stop; the message says what to do
    printf '%sSTOPPED:%s %s\n' "${C_BAD}" "${C_END}" "$*"
    exit 1
}
usage() {
    say "usage: ${SELF} [status|dry-run|after-reboot]"
    say "       ${SELF} run [--yes] [--compose-bin PATH] [--checkout PATH] [--project NAME]"
    say "       ${SELF} firewall [test|remove] [--yes]"
    exit 64
}
ask() { # ask QUESTION: yes (0) or no (1); --yes answers yes; no answer (end of input) is no
    local answer=""
    if [ "${YES}" = 1 ]; then
        say "$1 [y/N] yes (--yes)"
        return 0
    fi
    printf '%s [y/N] ' "$1"
    read -r answer || {
        say ""
        say "(no answer: taken as no; give --yes to answer yes to every question)"
        return 1
    }
    case "${answer}" in [yY] | [yY][eE][sS]) return 0 ;; esac
    return 1
}
show_log() { # show_log PATH: a task's log, trimmed (the whole one stays in PATH)
    local log="$1" lines
    [ -f "${log}" ] || {
        say "  (its log ${log} is not there)"
        return 0
    }
    lines="$(wc -l <"${log}")"
    if [ "${lines}" -le 40 ]; then
        sed 's/^/  | /' "${log}"
    else
        head -n 5 "${log}" | sed 's/^/  | /'
        say "  | ... $((lines - 35)) lines left out: the whole log is ${log}"
        tail -n 30 "${log}" | sed 's/^/  | /'
    fi
}

# --- the plugin's runner -------------------------------------------------------------------------------
need_root() {
    [ "$(id -u)" = 0 ] || fail "run this as root: open Unraid's terminal (the >_ icon at the top right) and run it there"
}
need_runner() {
    [ -x "${RUNNER}" ] || fail "the MCPanel plugin is not installed (${RUNNER} is missing): Plugins > Install Plugin with the plugin's address (docs/UNRAID.md, \"The Unraid plugin\"), then run this again"
    [ -z "$(find "${RUNNER}" "$(dirname "${RUNNER}")" -maxdepth 0 \( -perm /022 -o ! -uid "${EUID}" \) 2>/dev/null)" ] ||
        fail "the plugin's runner (${RUNNER}) can be changed by others than root: reinstall the plugin (Plugins > MCPanel > Remove, then install it again), then run this again"
}
read_facts() { # the runner's facts into F (lists: one value per line)
    local line key value out
    F=()
    out="$("${RUNNER}" facts 2>/dev/null)" || return 1
    while IFS= read -r line; do
        key="${line%%=*}"
        value="${line#*=}"
        [[ "${key}" =~ ^[A-Z][A-Z_]*$ ]] || continue
        case "${key}" in
            LOG_FILE | USER_SCRIPT | UPDATE_COMMIT | DETECTED) F["${key}"]="${F["${key}"]:+${F["${key}"]}$'\n'}${value}" ;;
            *) F["${key}"]="${value}" ;;
        esac
    done <<<"${out}"
}
fact() { printf '%s' "${F["$1"]:-}"; }
facts_or_fail() {
    read_facts || fail "the plugin's runner did not answer (${RUNNER} facts): is Docker running? Then run this again"
}

# start_task TASK [OPTION]: start it through the runner; prints the log's path. Waits (bounded) for an action
# that runs already.
start_task() {
    local out rc waited=0
    while :; do
        out="$("${RUNNER}" start "$@" 2>&1)"
        rc=$?
        [ "${rc}" = 75 ] || break
        [ "${waited}" -lt 900 ] || fail "another action of the plugin has been running for 15 minutes (${out#BUSY: }): wait for it (the plugin's page shows it), then run this again"
        [ "${waited}" != 0 ] || say "  another action of the plugin is running (${out#BUSY: }): waiting for it (up to 15 minutes)..."
        sleep 5
        waited=$((waited + 5))
    done
    if [ "${rc}" != 0 ] || [[ "${out}" != STARTED:* ]]; then
        say "${out}" >&2
        return 1
    fi
    printf '%s\n' "${out##*, log }"
}

# wait_task LOG MINUTES WHAT: wait until the task that writes LOG has ended; its result in TASK_RESULT/TASK_EXIT/
# TASK_NEEDS of F. Ctrl-C ends only the waiting: the task goes on (as when the page is left).
WAITING_LOG=""
on_interrupt() {
    say ""
    if [ -n "${WAITING_LOG}" ]; then
        say "Stopped waiting. The action goes on in the background (the plugin runs it, not this script); its log is"
        say "${WAITING_LOG}. Run '${SELF} status' later to see how it ended."
    else
        say "Stopped. Nothing was left half-done: run '${SELF} status' to see where things are."
    fi
    exit 130
}
trap on_interrupt INT
wait_task() {
    local log="$1" limit=$(($2 * 60)) what="$3" waited=0 result
    WAITING_LOG="${log}"
    say "  waiting for ${what} (up to $2 minutes; Ctrl-C stops only the waiting)..."
    while :; do
        # the runner's last line of every task: "Finished: ok|failed ..."
        result="$(sed -n 's/^Finished: \([a-z]*\).*/\1/p' "${log}" 2>/dev/null | tail -n 1)"
        if [ -n "${result}" ]; then
            read_facts || true
            [ "$(fact TASK_LOG)" = "${log}" ] || F[TASK_NEEDS]=""
            F[TASK_RESULT]="${result}"
            break
        fi
        # every 10 s: has the task ended without that line (killed)?
        if [ $((waited % 10)) = 9 ] && read_facts && [ "$(fact BUSY)" = 0 ] &&
            ! grep -q '^Finished: ' "${log}" 2>/dev/null; then
            F[TASK_RESULT]=interrupted
            break
        fi
        if [ "${waited}" -ge "${limit}" ]; then
            WAITING_LOG=""
            say "  ${what} is still running after $2 minutes. It goes on in the background; its log is ${log}."
            say "  Run '${SELF} status' later; then run this again."
            exit 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    WAITING_LOG=""
}

# run_task TASK MINUTES WHAT [OPTION]: start, wait, show the log; 0 when it ended ok
run_task() {
    local task="$1" minutes="$2" what="$3" log
    shift 3
    log="$(start_task "${task}" "$@")" || fail "the plugin did not start '${task}' (above). Run '${SELF} status' and look at the plugin's page"
    wait_task "${log}" "${minutes}" "${what}"
    show_log "${log}"
    [ "$(fact TASK_RESULT)" = ok ]
}

# --- the checks ----------------------------------------------------------------------------------------
plugin_loose() { find "${PLUGIN_DIR}" -perm /022 -printf '%m %p\n' 2>/dev/null | head -n 5; }

compose_target() { # the file Docker's Compose link points to (empty: no link)
    local link="${R}/usr/local/lib/docker/cli-plugins/docker-compose"
    [ -L "${link}" ] || return 0
    readlink -f "${link}" 2>/dev/null || true
}
# The binary `run` would set: --compose-bin, else a file the link points to that is not the plugin's own copy
compose_candidate() {
    local target
    if [ -n "${COMPOSE_ARG}" ]; then
        [ "${COMPOSE_ARG}" = "$(fact SET_COMPOSE_BIN)" ] || printf '%s' "${COMPOSE_ARG}"
        return 0
    fi
    [ -z "$(fact SET_COMPOSE_BIN)" ] || return 0
    target="$(compose_target)"
    [ -n "${target}" ] && [ -f "${target}" ] && [ "${target}" != "$(readlink -f "${OWN_COMPOSE}" 2>/dev/null || printf '%s' "${OWN_COMPOSE}")" ] || return 0
    [ "${target}" != "$(fact REMEMBERED_COMPOSE_BIN)" ] || return 0
    printf '%s' "${target#"${R}"}"
}
compose_state() { # "OK|TO DO|NEEDS YOU" + "|" + why
    local cand
    cand="$(compose_candidate)"
    if [ -n "$(fact SET_COMPOSE_BIN)" ]; then
        if [ -x "$(fact SET_COMPOSE_BIN)" ]; then printf 'OK|set in the plugin: %s' "$(fact SET_COMPOSE_BIN)"; else printf 'NEEDS YOU|set in the plugin: %s, but it is not there (is its pool mounted?)' "$(fact SET_COMPOSE_BIN)"; fi
    elif [ -n "${cand}" ]; then
        printf 'TO DO|linked by hand to %s: not set in the plugin, gone after the next reboot' "${cand}"
    elif [ "$(fact OWN_COMPOSE)" = installed ]; then
        printf 'OK|the plugin'"'"'s copy (%s)' "$(fact OWN_COMPOSE_VERSION)"
    elif [ -n "$(fact REMEMBERED_COMPOSE_BIN)" ]; then
        printf 'OK|array-start.env remembers %s (linked at every boot)' "$(fact REMEMBERED_COMPOSE_BIN)"
    elif [ -n "$(fact COMPOSE)" ]; then
        printf 'OK|built in or from another plugin'
    else
        printf 'TO DO|none: the plugin can install the official release (run asks)'
    fi
}

checkout_line() {
    case "$(fact CHECKOUT_FROM)" in
        settings | detected)
            if [ -n "$(fact CHECKOUT_PROBLEM)" ]; then
                item "NEEDS YOU" "Checkout" "$(fact CHECKOUT_PROBLEM)"
            else
                item OK "Checkout" "$(fact CHECKOUT) ($(fact CHECKOUT_FROM)), project $(fact PROJECT) ($(fact PROJECT_FROM))"
            fi
            ;;
        ambiguous) item "NEEDS YOU" "Checkout" "Docker knows several MCPanel stacks: run '${SELF} run --checkout /path/to/mcpanel-src'" ;;
        *) item "NEEDS YOU" "Checkout" "none found (no settings, no running stack): run '${SELF} run --checkout /path/to/mcpanel-src'" ;;
    esac
}

firewall_state() { # "OK|..." or "TO DO|..." or "CANNOT TELL|..."
    local env
    env="$(fact CHECKOUT)/deploy/instance-firewall.env"
    [ -n "$(fact CHECKOUT)" ] || {
        printf 'CANNOT TELL|no checkout known'
        return
    }
    if grep -qx 'INSTALLED=yes' "${env}" 2>/dev/null; then
        if command -v iptables >/dev/null 2>&1 && ! iptables -S MCPANEL-FWD >/dev/null 2>&1; then
            printf 'TO DO|installed by you, but its rules are not loaded now: the boot tasks put them back'
        else
            printf 'OK|installed (put back at every boot by the boot tasks)'
        fi
    else
        printf 'OK|not installed (optional: %s firewall)' "${SELF}"
    fi
}

boot_epoch() { # when this boot began (seconds since 1970)
    local up
    up="$(cut -d' ' -f1 "${R}/proc/uptime" 2>/dev/null || cut -d' ' -f1 /proc/uptime)"
    printf '%s' "$(($(date +%s) - ${up%.*}))"
}
when() { date -d "@$1" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || printf '%s' "$1"; }

cmd_status() {
    local loose state why log
    facts_or_fail
    say "MCPanel first-run checklist (docs/UNRAID.md, \"The Unraid plugin\"); nothing is changed."
    say ""
    loose="$(plugin_loose)"
    if [ -z "$(fact PLUGIN_VERSION)" ]; then
        item "NEEDS YOU" "Plugin" "its runner answers, but /boot/config/plugins/mcpanel.plg is missing: install it through Plugins"
    elif [ -n "${loose}" ]; then
        item "NEEDS YOU" "Plugin files" "writable by others: ${loose//$'\n'/, } (reinstall the plugin)"
    else
        item OK "Plugin" "$(fact PLUGIN_VERSION), its files writable by root only"
    fi
    if [ -n "$(fact CHECKOUT_PLUGIN_VERSION)" ] && [[ "$(fact CHECKOUT_PLUGIN_VERSION)" > "$(fact PLUGIN_VERSION)" ]]; then
        item "TO DO" "Plugin update" "the checkout has $(fact CHECKOUT_PLUGIN_VERSION): Plugins > Check for updates"
    fi
    [ -z "$(fact RUN_DIR_PROBLEM)" ] || item "NEEDS YOU" "Run folder" "$(fact RUN_DIR_PROBLEM)"
    checkout_line
    IFS='|' read -r state why <<<"$(compose_state)"
    item "${state}" "Compose binary" "${why}"
    if [ -n "$(fact COMPOSE)" ]; then
        item OK "Docker Compose" "$(fact COMPOSE)$( [ -z "$(fact COMPOSE_LINK)" ] || printf ', linked to %s' "$(fact COMPOSE_LINK)")"
    else
        item "TO DO" "Docker Compose" "'docker compose' does not answer"
    fi
    if [ "$(fact HOOK)" = installed ] && [ "$(fact BOOT_EVENT)" = yes ]; then
        item OK "Shutdown hook" "installed (event/stopping_docker and event/docker_started)"
    elif [ "$(fact BOOT_EVENT)" != yes ]; then
        item "NEEDS YOU" "Event script" "event/docker_started is missing: reinstall the plugin"
    else
        item "TO DO" "Shutdown hook" "not installed yet: the boot tasks install it (run does them now)"
    fi
    if [ -n "$(fact LOOSE_DIRS)" ]; then
        item "TO DO" "Docker CLI folders" "writable by others ($(fact LOOSE_DIRS)): the boot tasks make them 755"
    else
        item OK "Docker CLI folders" "only root can write them"
    fi
    if [ -n "$(fact USER_SCRIPT)" ] || [ "$(fact GO_FILE)" = yes ]; then
        item "TO DO" "Also at boot" "$( [ -z "$(fact USER_SCRIPT)" ] || printf 'User Script %s; ' "$(fact USER_SCRIPT | tr '\n' ',' | sed 's/,$//')")$( [ "$(fact GO_FILE)" != yes ] || printf '/boot/config/go; ')take it out once the plugin works (both at once do no harm)"
    fi
    if [ -n "$(fact BOOT_NEEDS)" ]; then
        item "NEEDS YOU" "Last boot run" "$(fact BOOT_NEEDS)"
    elif [ "$(fact BOOT_RESULT)" = ok ]; then
        item OK "Last boot run" "ok at $(when "$(fact BOOT_ENDED)")$([ "$(fact BOOT_ENDED)" -ge "$(boot_epoch)" ] 2>/dev/null && printf ' (this boot)' || printf ' (before this boot)')"
    elif [ -n "$(fact BOOT_RESULT)" ]; then
        log="$(fact BOOT_LOG)"
        item "NEEDS YOU" "Last boot run" "$(fact BOOT_RESULT) at $(when "$(fact BOOT_ENDED)"): its log is ${log}"
    else
        item "TO DO" "Last boot run" "none since this boot or since the plugin was installed"
    fi
    IFS='|' read -r state why <<<"$(firewall_state)"
    item "${state}" "Firewall" "${why}"
    say ""
    if [ -n "$(fact BOOT_RESULT)" ] && [ "$(fact BOOT_RESULT)" = ok ] && [ "${ISSUES}" = 0 ]; then
        NEXT="${SELF} after-reboot   (after your next reboot; the firewall is optional: ${SELF} firewall)"
    elif [ "$(fact CHECKOUT_FROM)" = none ] || [ "$(fact CHECKOUT_FROM)" = ambiguous ]; then
        NEXT="${SELF} run --checkout /mnt/user/appdata/mcpanel-src   (the folder of your clone)"
    elif [ -n "$(fact CHECKOUT_PROBLEM)" ]; then
        NEXT="the repair named on the Checkout line, then: ${SELF} run"
    else
        NEXT="${SELF} run   (it shows what it does and asks before each change; '${SELF} dry-run' lists the commands)"
    fi
    say "Next: ${NEXT}"
}

# --- dry-run ------------------------------------------------------------------------------------------
describe() { # describe TASK [OPTION]: the runner's own description, indented
    "${RUNNER}" describe "$@" 2>&1 | sed '1{/^Runs as root/d}' | sed 's/^/    /'
}
cmd_dry_run() {
    local cand
    facts_or_fail
    say "What '${SELF} run' would have the plugin do now (nothing is changed):"
    say ""
    say "Step 1, checks only: the plugin's files, its run folder, the checkout (read-only)."
    say "Step 2, the settings:"
    cand="$(compose_candidate)"
    if [ -n "${cand}" ] || [ -n "${CHECKOUT_ARG}${PROJECT_ARG}" ]; then
        say "    $(printf '%q ' "${RUNNER#"${R}"}" save "${CHECKOUT_ARG:-$(fact SET_CHECKOUT)}" "${PROJECT_ARG:-$(fact SET_PROJECT)}" "${cand:-$(fact SET_COMPOSE_BIN)}")"
    elif [ -z "$(fact COMPOSE)" ] && [ "$(fact OWN_COMPOSE)" != installed ]; then
        say "    (Docker Compose does not answer: it would offer the plugin's Install Compose)"
        describe compose-install
    else
        say "    nothing to change"
    fi
    say "Step 3, the tasks that change nothing, one after the other:"
    for task in status check-updates boot-dry-run firewall-status; do
        say "  ${task}:"
        describe "${task}"
    done
    say "Step 4, the boot tasks now (what runs at every Docker start):"
    describe boot-run
    say ""
    say "Nothing was changed. Next: ${SELF} run"
}

# --- run ----------------------------------------------------------------------------------------------
step() { say ""; say "${C_DIM}==>${C_END} $*"; }
log_done() { # log_done TASK: a finished log of TASK exists (newest first)
    local name
    while IFS= read -r name; do
        [ -n "${name}" ] || continue
        case "${name}" in *"-$1.log")
            grep -q '^Finished: ' "${R}/run/mcpanel/logs/${name}" 2>/dev/null && return 0
            ;;
        esac
    done <<<"$(fact LOG_FILE)"
    return 1
}

run_step1() {
    local loose
    step "Step 1: the plugin is installed and only root can change it"
    [ -n "$(fact PLUGIN_VERSION)" ] || fail "the plugin is not registered (/boot/config/plugins/mcpanel.plg is missing): install it through Plugins > Install Plugin, then run this again"
    loose="$(plugin_loose)"
    [ -z "${loose}" ] || fail "the plugin's files can be changed by others (${loose//$'\n'/, }): reinstall it (Plugins > MCPanel > Remove, then install it again), then run this again"
    [ "$(fact BOOT_EVENT)" = yes ] || fail "the plugin's event script (event/docker_started) is missing: reinstall the plugin, then run this again"
    [ -z "$(fact RUN_DIR_PROBLEM)" ] || fail "$(fact RUN_DIR_PROBLEM)"
    item OK "Plugin" "$(fact PLUGIN_VERSION), its files writable by root only"
}

run_step2() {
    local cand checkout project bin out
    step "Step 2: the settings (checkout, Compose project, Compose binary)"
    cand="$(compose_candidate)"
    checkout="${CHECKOUT_ARG:-$(fact SET_CHECKOUT)}"
    project="${PROJECT_ARG:-$(fact SET_PROJECT)}"
    bin="${cand:-$(fact SET_COMPOSE_BIN)}"
    if [ -n "${cand}" ] || { [ -n "${CHECKOUT_ARG}" ] && [ "${CHECKOUT_ARG}" != "$(fact SET_CHECKOUT)" ]; } ||
        { [ -n "${PROJECT_ARG}" ] && [ "${PROJECT_ARG}" != "$(fact SET_PROJECT)" ]; }; then
        [ -z "${cand}" ] || say "  Docker's Compose link points to ${cand}, which the plugin does not know: after a reboot nothing would link it again."
        say "  The plugin would keep: checkout ${checkout:-(found from the running stack)}, project ${project:-(found from the running stack)}, Compose binary ${bin:-(none)}."
        if ask "  Save these settings in the plugin?"; then
            out="$("${RUNNER}" save "${checkout}" "${project}" "${bin}" 2>&1)" || fail "the plugin refused the settings: ${out#REFUSED: }"
            say "  ${out}"
        else
            fail "the settings were not saved. Give the binary with --compose-bin PATH (or none: the plugin's Install Compose), then run this again"
        fi
        facts_or_fail
    fi
    if [ -z "$(fact COMPOSE)" ] && [ -z "$(fact SET_COMPOSE_BIN)" ] && [ "$(fact OWN_COMPOSE)" != installed ]; then
        say "  Docker Compose does not answer and no binary is set. The plugin can install the official release"
        say "  ($(fact OWN_COMPOSE_VERSION), checked against the SHA-256 $(fact OWN_COMPOSE_SHA256)):"
        describe compose-install
        ask "  Install Compose through the plugin?" || fail "no Compose: run again with --compose-bin PATH, or answer yes to Install Compose"
        run_task compose-install 15 "Install Compose" || fail "Install Compose did not finish (its log above): $(fact TASK_NEEDS)"
        facts_or_fail
    fi
    case "$(fact CHECKOUT_FROM)" in
        none) fail "no MCPanel checkout is known (no settings, no running stack): run again with --checkout /path/to/mcpanel-src" ;;
        ambiguous) fail "Docker knows several MCPanel stacks: run again with --checkout /path/to/mcpanel-src" ;;
    esac
    [ -z "$(fact CHECKOUT_PROBLEM)" ] || fail "$(fact CHECKOUT_PROBLEM)"
    item OK "Checkout" "$(fact CHECKOUT) ($(fact CHECKOUT_FROM)), project $(fact PROJECT)"
    IFS='|' read -r state why <<<"$(compose_state)"
    [ "${state}" = OK ] || fail "Compose: ${why}"
    item OK "Compose binary" "${why}"
}

run_step3() {
    local task what
    step "Step 3: the plugin's tasks that change nothing (each log is shown)"
    for task in status check-updates boot-dry-run firewall-status; do
        case "${task}" in
            status) what="the full status" ;;
            check-updates) what="Check for updates (git fetch, never a pull)" ;;
            boot-dry-run) what="the boot tasks' dry run" ;;
            firewall-status) what="the firewall's status" ;;
        esac
        if log_done "${task}"; then
            item OK "${task}" "shown already (its log is under /run/mcpanel/logs)"
            continue
        fi
        say "  ${what}:"
        log="$(start_task "${task}")" || fail "the plugin did not start '${task}' (above)"
        wait_task "${log}" 5 "${what}"
        show_log "${log}"
        if grep -q '^REFUSED: ' "${log}" 2>/dev/null; then
            fail "$(grep -m 1 '^REFUSED: ' "${log}" | sed 's/^REFUSED: //')"
        fi
        if [ "${task}" = firewall-status ] && firewall_not_installed "${log}"; then
            item OK "${task}" "not installed: a normal state, the firewall is optional (log ${log})"
            continue
        fi
        case "$(fact TASK_RESULT)" in
            ok) item OK "${task}" "done (log ${log})" ;;
            *) item "TO DO" "${task}" "it reported something to look at (above; log ${log}); this does not stop the walk" ;;
        esac
    done
}

# firewall_not_installed LOG: the firewall's status said "not installed", a normal state (the plugin's runner
# from 2026.11.12b on reports it as ok; an older one as failed with exit 1 or 3)
firewall_not_installed() {
    grep -q -E '^Instance firewall: (not installed|missing \(not installed\))' "$1" 2>/dev/null
}

run_step4() {
    step "Step 4: the boot tasks now (Compose link, shutdown hook, firewall where installed, stack check)"
    if [ "$(fact HOOK)" = installed ] && [ "$(fact BOOT_RESULT)" = ok ] && [ -z "$(fact LOOSE_DIRS)" ]; then
        item OK "Boot tasks" "ran ok at $(when "$(fact BOOT_ENDED)"); the shutdown hook is installed"
        return 0
    fi
    describe boot-run
    ask "  Run the boot tasks now (what the plugin does at every Docker start)?" || fail "the boot tasks were not run: they run at the next Docker start; or run this again"
    run_task boot-run 15 "the boot tasks" || true
    facts_or_fail
    [ -z "$(fact BOOT_NEEDS)" ] || fail "$(fact BOOT_NEEDS)"
    [ "$(fact BOOT_RESULT)" = ok ] || fail "the boot tasks ended '$(fact BOOT_RESULT)' (their log above): fix what it names, then run this again"
    [ "$(fact HOOK)" = installed ] && [ -x "${PLUGIN_DIR}/event/stopping_docker" ] && [ -x "${PLUGIN_DIR}/event/docker_started" ] ||
        fail "the boot tasks ran, but the shutdown hook is not in ${PLUGIN_DIR}/event: look at their log above"
    item OK "Boot tasks" "ran ok; the shutdown hook is installed (event/stopping_docker, event/docker_started)"
    [ -z "$(fact LOOSE_DIRS)" ] || item "TO DO" "Docker CLI folders" "still writable by others: $(fact LOOSE_DIRS)"
}

cmd_run() {
    facts_or_fail
    say "MCPanel first-run checklist, steps 1 to 4 (docs/UNRAID.md). Each step that changes something asks first."
    run_step1
    run_step2
    run_step3
    run_step4
    say ""
    say "Steps 1 to 4 are done. Take out a User Script or /boot/config/go line that does the same (status lists them)."
    say "Next: reboot when it suits you, then: ${SELF} after-reboot"
}

# --- after a reboot -------------------------------------------------------------------------------------
cmd_after_reboot() {
    local since lines project id line service running nets name
    facts_or_fail
    say "Step 5 of the checklist: after a reboot (nothing is changed)."
    say ""
    since="$(boot_epoch)"
    if [ -n "$(fact BOOT_NEEDS)" ]; then
        item "NEEDS YOU" "Boot tasks" "$(fact BOOT_NEEDS)"
    elif [ "$(fact BOOT_RESULT)" = ok ] && [ "$(fact BOOT_ENDED)" -ge "${since}" ] 2>/dev/null; then
        item OK "Boot tasks" "ran ok at this boot, $(when "$(fact BOOT_ENDED)") (booted $(when "${since}"))"
    elif [ -n "$(fact BOOT_RESULT)" ] && [ "$(fact BOOT_ENDED)" -ge "${since}" ] 2>/dev/null; then
        item "NEEDS YOU" "Boot tasks" "$(fact BOOT_RESULT) at $(when "$(fact BOOT_ENDED)"): their log is $(fact BOOT_LOG)"
    else
        item "TO DO" "Boot tasks" "no run since this boot ($(when "${since}")): has Docker started? The plugin runs them at every Docker start"
    fi
    lines="$(grep -a -E ' mcpanel(\[[0-9]+\])?: ' "${SYSLOG}" 2>/dev/null | tail -n 15)"
    if [ -n "${lines}" ]; then
        item OK "Syslog" "the mcpanel lines (the newest 15):"
        say "${lines}" | sed 's/^/  | /'
    else
        item "CANNOT TELL" "Syslog" "no mcpanel line in ${SYSLOG#"${R}"}"
    fi
    if [ "$(fact HOOK)" = installed ]; then item OK "Shutdown hook" "installed"; else item "TO DO" "Shutdown hook" "not installed: ${SELF} run"; fi
    project="$(fact PROJECT)"
    if ! docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
        item "CANNOT TELL" "Stack" "Docker does not answer"
    else
        for id in $(docker ps -a -q --filter "label=com.docker.compose.project=${project:-mcpanel}" 2>/dev/null); do
            line="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}|{{.State.Running}}|{{len .NetworkSettings.Networks}}|{{.Name}}|{{index .Config.Labels "com.docker.compose.oneoff"}}' "${id}" 2>/dev/null)" || continue
            IFS='|' read -r service running nets name _ <<<"${line}"
            case "${line##*|}" in [Tt]rue) continue ;; esac
            case "${service}" in panel | agent | postgres) ;; *) continue ;; esac
            if [ "${running}" = true ] && [ "${nets}" -gt 0 ] 2>/dev/null; then
                item OK "${name#/}" "running, on ${nets} network(s)"
            else
                item "NEEDS YOU" "${name#/}" "$([ "${running}" = true ] && printf 'running without a network' || printf 'not running'): the agent repairs a lost network within a minute; else docs/UNRAID.md, section 9"
            fi
        done
    fi
    say ""
    say "Step 6 (when it suits you): Settings > Docker > Enable Docker: No, Apply; then Yes, Apply. Then run this"
    say "again: if 'Boot tasks' does not say 'this boot', Unraid does not fire the Docker events then (press the page's"
    say "Boot tasks > Run now after such a restart). Step 7: the first update through the plugin's page."
    say "Next: $([ "${ISSUES}" = 0 ] && printf '%s firewall   (optional)' "${SELF}" || printf 'what the lines above name, then %s after-reboot' "${SELF}")"
}

# --- the firewall ---------------------------------------------------------------------------------------
selftest_image() { # a tiny image with nc that is on this host already: alpine or busybox
    docker image ls --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -E '^(alpine|busybox):[A-Za-z0-9._-]+$' | head -n 1
}
instance_network() {
    local name
    name="$(docker network ls --filter "label=com.docker.compose.project=$(fact PROJECT)" \
        --filter label=com.docker.compose.network=instances --format '{{.Name}}' 2>/dev/null | head -n 1)"
    printf '%s' "${name:-mcpanel_instances}"
}
server_network() { # one network of a game server of its own (the agent's labels and name), if one exists now
    docker network ls --filter label=io.mcpanel.managed=true --filter label=io.mcpanel.network=server \
        --format '{{.Name}}' 2>/dev/null | grep -E '^zz_mcpanel_i_[0-9a-f]{12}$' | sort | head -n 1
}
cleanup_selftest() { docker rm -f "${SELFTEST}" >/dev/null 2>&1 || true; }

# probe NETWORK TARGET...: "host:port open|closed" lines, from a container on NETWORK (the targets go in as
# arguments, never into the script text)
probe() {
    local net="$1"
    shift
    cleanup_selftest
    trap 'cleanup_selftest; on_interrupt' INT
    trap 'cleanup_selftest; exit 143' TERM
    trap cleanup_selftest EXIT
    # shellcheck disable=SC2016 # the script runs in the container; the targets are its arguments
    timeout 120 docker run --rm --name "${SELFTEST}" --network "${net}" --entrypoint sh "${IMAGE}" -c \
        'for t in "$@"; do if nc -z -w 4 "${t%:*}" "${t##*:}" 2>/dev/null; then echo "$t open"; else echo "$t closed"; fi; done' \
        sh "$@" 2>/dev/null
    cleanup_selftest
    trap on_interrupt INT
    trap - EXIT TERM
}
host_reaches() { # host_reaches URL: this host gets an answer of any kind (a connection was made)
    local rc
    curl -s -k -m 5 -o /dev/null "$1"
    rc=$?
    case "${rc}" in 6 | 7 | 28) return 1 ;; esac
    return 0
}

# selftest_targets: HOST_UI (this server's web UI), LAN (a private address that answers this host), INET_IP, INET_NAME
selftest_targets() {
    local ip gw port
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1)"
    gw="$(ip -4 route show default 2>/dev/null | sed -n 's/^default via \([0-9.]*\).*/\1/p' | head -n 1)"
    port="$(sed -n 's/^PORT="\{0,1\}\([0-9]\{1,5\}\)"\{0,1\}\r\{0,1\}$/\1/p' "${IDENT}" 2>/dev/null | tail -n 1)"
    HOST_UI=""
    [ -z "${ip}" ] || HOST_UI="${ip}:${port:-80}"
    LAN=""
    if [ -n "${gw}" ]; then
        if host_reaches "http://${gw}/"; then LAN="${gw}:80"; elif host_reaches "https://${gw}/"; then LAN="${gw}:443"; fi
    fi
    INET_IP="1.1.1.1:443"
    INET_NAME="github.com:443"
}

# the results per network: shared (S) and a server's own (P), before and after the install
# shellcheck disable=SC2034 # used through name references (probe_into, judge)
declare -A BEFORE_S=() AFTER_S=() BEFORE_P=() AFTER_P=()
probe_into() { # probe_into ARRAY-NAME NETWORK
    local line target state
    local -n into="$1"
    into=()
    while IFS= read -r line; do
        target="${line% *}"
        state="${line##* }"
        # shellcheck disable=SC2034 # a name reference: it fills one of the arrays above
        [ -n "${target}" ] && into["${target}"]="${state}"
    done <<<"$(probe "$2" ${HOST_UI:+"${HOST_UI}"} ${LAN:+"${LAN}"} "${INET_IP}" "${INET_NAME}")"
}
probe_all() { # probe_all before|after
    if [ "$1" = before ]; then
        probe_into BEFORE_S "${NET}"
        [ -z "${SNET}" ] || probe_into BEFORE_P "${SNET}"
    else
        probe_into AFTER_S "${NET}"
        [ -z "${SNET}" ] || probe_into AFTER_P "${SNET}"
    fi
}

# judge NETWORK WHAT BEFORE AFTER: PASS/FAIL per check of one network; MUST_WORK_FAILED=1 when the internet does
# not work with the firewall, PROTECTS=0 when something is still reachable
judge() {
    local t net="$1" what="$2"
    local -n before="$3" after="$4"
    say ""
    say "Self-test from a container on ${net} (${what}: what a game server there sees):"
    for t in ${HOST_UI:+"${HOST_UI}"} ${LAN:+"${LAN}"}; do
        if [ "${#before[@]}" -gt 0 ] && [ "${before[${t}]:-}" = closed ]; then
            item "CANNOT TELL" "must fail" "${t}: not reachable from a container before the firewall either"
        elif [ "${after[${t}]:-}" = closed ]; then
            item PASS "must fail" "${t}$([ "${t}" = "${HOST_UI}" ] && printf ' (this server'"'"'s web UI)' || printf ' (a private address on your LAN)'): blocked"
        elif [ -z "${after[${t}]:-}" ]; then
            item "CANNOT TELL" "must fail" "${t}: the test container did not answer"
        else
            item FAIL "must fail" "${t}: still reachable from a game server"
            PROTECTS=0
        fi
    done
    [ -n "${LAN}" ] || item "CANNOT TELL" "must fail" "no private address that answers this host was found (the router's web page): not tested"
    [ -n "${HOST_UI}" ] || item "CANNOT TELL" "must fail" "this server's address was not found: not tested"
    for t in "${INET_IP}" "${INET_NAME}"; do
        if [ "${after[${t}]:-}" = open ]; then
            item PASS "must work" "${t} (the internet$([ "${t}" = "${INET_NAME}" ] && printf ', by name: DNS works')): reachable"
        elif [ "${#before[@]}" -gt 0 ] && [ "${before[${t}]:-}" != open ]; then
            item "CANNOT TELL" "must work" "${t}: not reachable before the firewall either (no internet?)"
        else
            item FAIL "must work" "${t}: NOT reachable: game servers could not download or check accounts"
            MUST_WORK_FAILED=1
        fi
    done
}
judge_all() {
    MUST_WORK_FAILED=0
    PROTECTS=1
    judge "${NET}" "the shared instance network" BEFORE_S AFTER_S
    if [ -n "${SNET}" ]; then
        judge "${SNET}" "a game server's own network" BEFORE_P AFTER_P
    else
        say ""
        item "CANNOT TELL" "server network" "no server network exists right now: start a server and run this test again to check that range too"
    fi
}

firewall_prepare() {
    local pull
    [ "$(fact CHECKOUT_FROM)" = settings ] || [ "$(fact CHECKOUT_FROM)" = detected ] || fail "no MCPanel checkout is known: ${SELF} run first"
    docker version --format '{{.Server.Version}}' >/dev/null 2>&1 || fail "Docker does not answer: start it (Settings > Docker), then run this again"
    NET="$(instance_network)"
    docker network inspect "${NET}" >/dev/null 2>&1 || fail "the instance network ${NET} does not exist: start the stack (docker compose up -d in deploy/), then run this again"
    SNET="$(server_network)"
    IMAGE="$(selftest_image)"
    if [ -z "${IMAGE}" ]; then
        say "The self-test needs a tiny image with 'nc' (alpine or busybox); none is on this host."
        ask "Download alpine:3 (about 4 MB) for it?" || fail "no self-test without an image: answer yes, or 'docker pull alpine:3' yourself, then run this again"
        pull="$(docker pull -q alpine:3 2>&1)" || fail "could not download alpine:3 (${pull}): check the internet connection, then run this again"
        IMAGE=alpine:3
    fi
    selftest_targets
}

firewall_remove() {
    say ""
    say "Removing the firewall through the plugin:"
    describe firewall-remove
    run_task firewall-remove 5 "the firewall's removal" || fail "the removal did not finish (its log above): look at the plugin's page"
    item OK "Firewall" "removed"
}

cmd_firewall() {
    local sub="${1:-install}"
    facts_or_fail
    say "The game servers' firewall (deploy/unraid/instance-firewall.sh): it keeps game containers away from this"
    say "server and your LAN. Seen on one Unraid 7.3.4 server (installed, the self-test from the shared network);"
    say "not seen there yet: from a server's own network, and the rules put back at a boot. The self-test shows it."
    case "${sub}" in
        remove)
            ask "Remove the firewall?" || fail "nothing was changed"
            firewall_remove
            say "Next: ${SELF} status"
            return 0
            ;;
        test)
            firewall_prepare
            say "Testing with ${IMAGE} on ${NET}${SNET:+ and ${SNET}} (container ${SELFTEST}, removed afterwards)..."
            probe_all after
            judge_all
            say ""
            say "Next: $([ "${MUST_WORK_FAILED}" = 1 ] && printf '%s firewall remove   (game servers cannot reach the internet)' "${SELF}" || printf '%s status' "${SELF}")"
            return 0
            ;;
        install) ;;
        *) usage ;;
    esac
    firewall_prepare
    say ""
    run_task firewall-status 5 "the firewall's status" || true
    run_task firewall-dry-run 5 "the firewall's dry run" || fail "the dry run did not finish (above)"
    say ""
    say "Testing before (with ${IMAGE} on ${NET}${SNET:+ and ${SNET}}, container ${SELFTEST}, removed afterwards)..."
    probe_all before
    ask "Install the firewall now (the commands of the dry run above)?" || fail "nothing was changed. Next: ${SELF} status"
    run_task firewall-install 5 "the firewall's installation" || fail "the installation did not finish (its log above); nothing else was changed. Next: ${SELF} firewall remove, then look at the log"
    probe_all after
    judge_all
    say ""
    if [ "${MUST_WORK_FAILED}" = 1 ]; then
        say "The internet does not work from a game server with the firewall: taking it out again."
        YES=1 firewall_remove
        say "Next: tell the developers what the self-test showed; ${SELF} status"
        return 1
    fi
    [ "${PROTECTS}" = 1 ] || say "The firewall does not block everything it should (FAIL above): it does no harm, but it does not protect."
    if ask "Keep the firewall (it is put back at every boot)?"; then
        say "Kept. Next: ${SELF} status"
    else
        firewall_remove
        say "Next: ${SELF} status"
    fi
}

# --- arguments ------------------------------------------------------------------------------------------
mode="${1:-status}"
[ "$#" = 0 ] || shift
sub=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --yes) YES=1 ;;
        --compose-bin | --checkout | --project)
            [ "$#" -ge 2 ] || usage
            case "$1" in
                --compose-bin) COMPOSE_ARG="$2" ;;
                --checkout) CHECKOUT_ARG="$2" ;;
                --project) PROJECT_ARG="$2" ;;
            esac
            shift
            ;;
        test | remove) [ "${mode}" = firewall ] && [ -z "${sub}" ] || usage; sub="$1" ;;
        *) usage ;;
    esac
    shift
done
case "${mode}" in status | dry-run | after-reboot) [ -z "${COMPOSE_ARG}${CHECKOUT_ARG}${PROJECT_ARG}" ] || [ "${mode}" = dry-run ] || usage ;; esac
for value in "${COMPOSE_ARG}" "${CHECKOUT_ARG}"; do
    [ -z "${value}" ] || [[ "${value}" == /* ]] || fail "paths must be absolute: ${value}"
done

need_root
need_runner
# reading must not touch the checkout either (the runner's git status would refresh its index otherwise)
export GIT_OPTIONAL_LOCKS=0
case "${mode}" in
    status) cmd_status ;;
    dry-run) cmd_dry_run ;;
    run) cmd_run ;;
    after-reboot) cmd_after_reboot ;;
    firewall) cmd_firewall "${sub:-install}" ;;
    *) usage ;;
esac
