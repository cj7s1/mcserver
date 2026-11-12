#!/bin/bash
# Host firewall rules for the game server containers (security review 2026-11-11, A-2).
#
#   bash instance-firewall.sh install [OPTIONS]   put the rules in place (as root); again at every array start
#   bash instance-firewall.sh status  [OPTIONS]   installed / missing / differs, and what the rules cover; exit 0
#                                                 installed and up to date, 3 not installed (a normal state: you
#                                                 never installed it or removed it), 1 differs or missing although
#                                                 installed
#   bash instance-firewall.sh dry-run [OPTIONS]   print the exact commands `install` would run; changes nothing
#   bash instance-firewall.sh remove              take the rules out again (as root)
#
# Options of install, status and dry-run:
#   --allow CIDR[:PORT]  let the game containers open connections to this private address or range
#                        (with :PORT only to that TCP and UDP port), e.g. a database on the LAN.
#                        `install` stores it, and every later `install` (the one at boot) repeats it.
#   --allow-dns IP       a DNS server the containers use besides the LAN resolvers of /etc/resolv.conf
#                        (stored like --allow)
#   --forget ENTRY       drop a stored --allow or --allow-dns entry (install)
#   --env FILE           the stack's env file (default: deploy/.env; on an additional node: node.env)
#   --network NAME       the shared instance network (default: AGENT_INSTANCE_NETWORK of the env file,
#                        else mcpanel_instances)
#   --wait SECONDS       install: wait up to SECONDS for Docker's DOCKER-USER chain and the shared
#                        network to appear (at array start Docker may not be up yet)
#
# ***************************************************************************************************
# RAN ON ONE REAL UNRAID 7.3.4 SERVER ON 2026-11-12 (through the MCPanel plugin and first-run.sh): `status`
# (not installed), `dry-run` and `install`; then, from a container on the shared instance network, the
# server's web UI and the router were blocked and the internet worked by address and by name, and the stack
# stayed healthy. NOT SEEN THERE YET: the rules put back at a boot, `remove`, `--allow`/`--allow-dns`, a
# container on a per-server network (the pool range), players joining with the rules in place. Elsewhere the
# rule set was loaded with iptables-restore in a container's own network namespace, and the script runs in
# tests against stand-ins. After `install`, check it from inside a game container (first-run.sh firewall
# test does it; or Docker tab -> the server's container -> Console, or `docker exec -it <container> sh`):
#   * the host's web UI and a LAN address must FAIL:  curl -m 5 http://<unraid LAN IP>/   and any LAN device
#   * the internet must WORK:                          curl -m 5 https://example.com/
#   * players must still join the game, from the LAN and from the internet
# and `bash instance-firewall.sh status` must say "installed". If something a server needs stops
# working, `bash instance-firewall.sh remove` takes everything out at once.
# ***************************************************************************************************
#
# What it does. Game containers run code you do not control. Docker lets them open connections to the
# host on every address (its web UI, SMB, every port other containers publish) and to the whole LAN.
# These rules drop NEW connections from the game containers' addresses
#   * to the host itself (chain MCPANEL-IN, jumped to from INPUT), and
#   * to private and link-local ranges: 10/8, 172.16/12, 192.168/16, 169.254/16, 100.64/10 (the last is
#     Tailscale's and carrier-grade NAT's) (chain MCPANEL-FWD, jumped to from DOCKER-USER),
# except
#   * replies (conntrack ESTABLISHED,RELATED): players keep working, from the LAN too;
#   * traffic inside the same network range, which is a game container and the agent on its bridge
#     (Docker's own isolation still drops traffic between two bridges of the pool);
#   * DNS (53, UDP and TCP) to the resolvers of /etc/resolv.conf that are LAN addresses, and to
#     --allow-dns servers;
#   * the --allow entries.
# Internet egress stays open (Minecraft checks accounts with Mojang, the Steam images download the game).
# The game containers' addresses: the per-server pool (AGENT_INSTANCE_POOL, default 10.213.0.0/16; every
# server gets a /29 of it) and the subnet of the shared instance network (asked from Docker), matched by
# source address, since each per-server bridge has an interface name of its own. IPv4 only: when one of
# these networks has IPv6, the script says that IPv6 is NOT restricted.
#
# install loads both chains in one iptables-restore (--noflush: nothing else is touched), records two
# fingerprints as the names of two empty chains, MCPANEL-C-<settings> and MCPANEL-A-<rules as loaded>
# (no extra kernel module needed, unlike comment rules), and inserts each jump once, at the top. status
# compares both; remove deletes the jumps, the chains and the two markers. iptables keeps
# rules in memory only: Unraid forgets them at every boot. `install` records INSTALLED=yes in its state
# file (deploy/instance-firewall.env) and `remove` INSTALLED=no: deploy/unraid/array-start.sh, run at every
# array start, installs the rules again where they were installed (docs/UNRAID.md, "The game servers'
# firewall"). Docker never touches DOCKER-USER or other chains.
#
# Root runs this script. When group or others can write it, its folder, its state file or the env file,
# `install` and `remove` run as root from the MCPanel checkout tighten the checkout (chmod -R go-w) and
# say so; otherwise they refuse and print the repair. `status` and `dry-run` only warn.
#
# MCPANEL_FIREWALL_RESOLV is for trying this script out somewhere else; leave it unset on Unraid.
set -u
umask 022

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="${HERE}/$(basename "${BASH_SOURCE[0]}")"
DEPLOY="$(cd "${HERE}/.." && pwd)"
STATE_FILE="${DEPLOY}/instance-firewall.env"
RESOLV="${MCPANEL_FIREWALL_RESOLV:-/etc/resolv.conf}"
FWD=MCPANEL-FWD
IN=MCPANEL-IN
MARK=MCPANEL-   # the marker chains: MCPANEL-C-<16 hex>, MCPANEL-A-<16 hex>
DEFAULT_POOL=10.213.0.0/16
DEFAULT_NETWORK=mcpanel_instances
PRIVATE=(10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10)

ENV_FILE="${DEPLOY}/.env"
NETWORK=""
WAIT=0
ADD_ALLOW=()
ADD_DNS=()
FORGET=()
POOL=""
SOURCES=()
DNS=()
ALLOWS=()
NOTES=()
IPV6=()

say() { printf '%s\n' "$*"; }
warn() { printf 'instance-firewall: WARNING: %s\n' "$*" >&2; }
fail() {
    printf 'instance-firewall: %s\n' "$*" >&2
    exit 1
}
usage() {
    say "usage: bash instance-firewall.sh install|status|dry-run [--allow CIDR[:PORT]] [--allow-dns IP] [--forget ENTRY]"
    say "                                 [--env FILE] [--network NAME] [--wait SECONDS]"
    say "       bash instance-firewall.sh remove"
    exit 64
}

# --- values -----------------------------------------------------------------------------------------
# cidr4 TEXT: "a.b.c.d" or "a.b.c.d/n" as "a.b.c.d/n" (an address is /32); fails for anything else
cidr4() {
    local ip="${1%/*}" bits=32 octet out=""
    [[ "$1" == */* ]] && bits="${1#*/}"
    [[ "${ip}" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || return 1
    [[ "${bits}" =~ ^[0-9]{1,2}$ ]] && [ "$((10#${bits}))" -le 32 ] || return 1
    for octet in ${ip//./ }; do
        [ "$((10#${octet}))" -le 255 ] || return 1
        out="${out:+${out}.}$((10#${octet}))" # no leading zeros: iptables would read them as octal
    done
    printf '%s/%s\n' "${out}" "$((10#${bits}))"
}

# allow_entry TEXT: "CIDR" or "CIDR:PORT", normalised; fails for anything else
allow_entry() {
    local cidr="$1" port=""
    if [[ "$1" == *:* ]]; then
        cidr="${1%%:*}"
        port="${1#*:}"
        [[ "${port}" =~ ^[0-9]{1,5}$ ]] && [ "$((10#${port}))" -ge 1 ] && [ "$((10#${port}))" -le 65535 ] || return 1
        port=":$((10#${port}))"
    fi
    cidr="$(cidr4 "${cidr}")" || return 1
    printf '%s%s\n' "${cidr}" "${port}"
}

# env_value KEY FILE: the last KEY=value of an env file, without quotes and a Windows line end
env_value() {
    local line value=""
    [ -r "$2" ] || return 0
    while IFS= read -r line || [ -n "${line}" ]; do
        case "${line}" in "$1="*) value="${line#*=}" ;; esac
    done <"$2"
    value="${value%$'\r'}"
    value="${value#\"}"
    value="${value%\"}"
    value="${value#\'}"
    value="${value%\'}"
    printf '%s\n' "${value}"
}

in_list() { # in_list WORD LIST...
    local word="$1" item
    shift
    for item in "$@"; do [ "${item}" = "${word}" ] && return 0; done
    return 1
}

private4() { # private4 IP: in a private, link-local or carrier-grade NAT range (not loopback)
    local a b
    IFS=. read -r a b _ _ <<<"$1"
    [ "${a}" = 10 ] && return 0
    [ "${a}" = 172 ] && [ "${b}" -ge 16 ] && [ "${b}" -le 31 ] && return 0
    [ "${a}" = 192 ] && [ "${b}" = 168 ] && return 0
    [ "${a}" = 169 ] && [ "${b}" = 254 ] && return 0
    [ "${a}" = 100 ] && [ "${b}" -ge 64 ] && [ "${b}" -le 127 ] && return 0
    return 1
}

# --- what the rules cover ---------------------------------------------------------------------------
read_config() {
    local value name subnets subnet v6 entry key line id
    [ -z "${ENV_FILE}" ] || [ -f "${ENV_FILE}" ] || NOTES+=("the env file ${ENV_FILE} does not exist: the defaults are used")
    value="$(env_value AGENT_INSTANCE_POOL "${ENV_FILE}")"
    POOL="$(cidr4 "${value:-${DEFAULT_POOL}}")" || fail "AGENT_INSTANCE_POOL in ${ENV_FILE} is not an IPv4 network (a.b.c.d/n): '${value}'"
    value="$(env_value AGENT_PER_SERVER_NETWORKS "${ENV_FILE}")"
    case "${value}" in
        0 | [Ff]alse | [Nn]o | [Oo]ff) NOTES+=("AGENT_PER_SERVER_NETWORKS is off: the agent puts every server on the shared network; the pool is covered anyway") ;;
    esac
    SOURCES=("${POOL}")
    [ -n "${NETWORK}" ] || NETWORK="$(env_value AGENT_INSTANCE_NETWORK "${ENV_FILE}")"
    NETWORK="${NETWORK:-${DEFAULT_NETWORK}}"
    [[ "${NETWORK}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || fail "not a Docker network name: '${NETWORK}'"
    if ! command -v docker >/dev/null 2>&1; then
        NOTES+=("docker is not on PATH: the shared network ${NETWORK} could not be asked, only the pool is covered")
    elif ! subnets="$(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}|{{.EnableIPv6}}' "${NETWORK}" 2>/dev/null)"; then
        NOTES+=("the shared network ${NETWORK} does not exist (yet): only the pool is covered; run install again once the stack is up")
    else
        v6="${subnets##*|}"
        for subnet in ${subnets%|*}; do
            if value="$(cidr4 "${subnet}")"; then
                in_list "${value}" "${SOURCES[@]}" || SOURCES+=("${value}")
            else
                v6=true
            fi
        done
        [ "${v6}" != true ] || IPV6+=("${NETWORK}")
        # the per-server networks the agent made (label io.mcpanel.managed=true)
        for id in $(docker network ls -q --filter label=io.mcpanel.managed=true 2>/dev/null); do
            line="$(docker network inspect -f '{{.Name}}|{{.EnableIPv6}}' "${id}" 2>/dev/null)" || continue
            [ "${line##*|}" != true ] || in_list "${line%|*}" "${IPV6[@]+"${IPV6[@]}"}" || IPV6+=("${line%|*}")
        done
    fi
    # LAN resolvers of the host: Docker's DNS asks them from the container's own network
    DNS=()
    if [ -r "${RESOLV}" ]; then
        while read -r key value _; do
            [ "${key}" = nameserver ] || continue
            value="$(cidr4 "${value}")" || continue # IPv6 resolvers: IPv6 is not restricted here
            value="${value%/32}"
            private4 "${value}" || continue
            in_list "${value}" "${DNS[@]+"${DNS[@]}"}" || DNS+=("${value}")
        done <"${RESOLV}"
    fi
    # stored entries, then this command's
    ALLOWS=()
    local -a stored_dns=()
    if [ -f "${STATE_FILE}" ]; then
        while IFS= read -r line || [ -n "${line}" ]; do
            case "${line}" in
                ALLOW=*)
                    if entry="$(allow_entry "${line#ALLOW=}")"; then ALLOWS+=("${entry}"); else warn "ignored in ${STATE_FILE}: ${line}"; fi
                    ;;
                ALLOW_DNS=*)
                    if entry="$(cidr4 "${line#ALLOW_DNS=}")"; then stored_dns+=("${entry%/32}"); else warn "ignored in ${STATE_FILE}: ${line}"; fi
                    ;;
            esac
        done <"${STATE_FILE}"
    fi
    for entry in "${ADD_ALLOW[@]+"${ADD_ALLOW[@]}"}"; do
        in_list "${entry}" "${ALLOWS[@]+"${ALLOWS[@]}"}" || ALLOWS+=("${entry}")
    done
    for entry in "${ADD_DNS[@]+"${ADD_DNS[@]}"}"; do
        in_list "${entry}" "${stored_dns[@]+"${stored_dns[@]}"}" || stored_dns+=("${entry}")
    done
    STORED_ALLOWS=()
    for entry in "${ALLOWS[@]+"${ALLOWS[@]}"}"; do
        in_list "${entry}" "${FORGET[@]+"${FORGET[@]}"}" || STORED_ALLOWS+=("${entry}")
    done
    ALLOWS=("${STORED_ALLOWS[@]+"${STORED_ALLOWS[@]}"}")
    STORED_DNS=()
    for entry in "${stored_dns[@]+"${stored_dns[@]}"}"; do
        in_list "${entry}" "${FORGET[@]+"${FORGET[@]}"}" || STORED_DNS+=("${entry}")
        in_list "${entry}" "${FORGET[@]+"${FORGET[@]}"}" || in_list "${entry}" "${DNS[@]+"${DNS[@]}"}" || DNS+=("${entry}")
    done
}

# The rules (iptables-restore format) of both chains, without the fingerprints.
rules() {
    local s d a cidr port proto chain
    say "-A ${FWD} -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN"
    say "-A ${IN} -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN"
    for s in "${SOURCES[@]}"; do
        say "-A ${FWD} -s ${s} -d ${s} -j RETURN"
    done
    for chain in "${FWD}" "${IN}"; do
        for s in "${SOURCES[@]}"; do
            for d in "${DNS[@]+"${DNS[@]}"}"; do
                for proto in udp tcp; do
                    say "-A ${chain} -s ${s} -d ${d}/32 -p ${proto} -m ${proto} --dport 53 -j RETURN"
                done
            done
            for a in "${ALLOWS[@]+"${ALLOWS[@]}"}"; do
                cidr="${a%%:*}"
                if [[ "${a}" == *:* ]]; then
                    port="${a#*:}"
                    for proto in tcp udp; do
                        say "-A ${chain} -s ${s} -d ${cidr} -p ${proto} -m ${proto} --dport ${port} -j RETURN"
                    done
                else
                    say "-A ${chain} -s ${s} -d ${cidr} -j RETURN"
                fi
            done
        done
    done
    for s in "${SOURCES[@]}"; do
        for d in "${PRIVATE[@]}"; do
            say "-A ${FWD} -s ${s} -d ${d} -j DROP"
        done
    done
    for s in "${SOURCES[@]}"; do
        say "-A ${IN} -s ${s} -j DROP"
    done
}

config_hash() { rules | sha256sum | cut -c1-16; }

# What install hands to iptables-restore: both chains made (or emptied) and filled, and the marker of
# the settings made, in one step.
restore_text() {
    say "*filter"
    say ":${FWD} - [0:0]"
    say ":${IN} - [0:0]"
    say ":${MARK}C-$(config_hash) - [0:0]"
    say "-F ${FWD}"
    say "-F ${IN}"
    rules
    say "COMMIT"
}

# --- what is installed ------------------------------------------------------------------------------
chain_exists() { iptables -S "$1" >/dev/null 2>&1; }
jumps() { iptables -S "$1" 2>/dev/null | grep -c -x -- "-A $1 -j $2"; } # how often CHAIN jumps to TARGET
snapshot_hash() { { iptables -S "${FWD}"; iptables -S "${IN}"; } 2>/dev/null | sha256sum | cut -c1-16; }
markers() { # markers C|A: the fingerprints in the names of the marker chains, one per line
    iptables -S 2>/dev/null | sed -n "s/^-N ${MARK}$1-\([0-9a-f]\{16\}\)\$/\1/p"
}
drop_markers() { # drop_markers C|A [KEEP]: delete the marker chains of that kind but KEEP
    local hash
    for hash in $(markers "$1"); do
        [ "${hash}" = "${2:-}" ] || iptables -X "${MARK}$1-${hash}" 2>/dev/null
    done
    return 0
}

ensure_jump() { # ensure_jump CHAIN TARGET: exactly one jump, inserted at the top when missing
    local count
    count="$(jumps "$1" "$2")"
    if [ "${count}" = 0 ]; then
        iptables -I "$1" 1 -j "$2" || fail "could not add the jump from $1 to $2"
    fi
    while [ "$(jumps "$1" "$2")" -gt 1 ]; do
        iptables -D "$1" -j "$2" || break
    done
}

need_root() {
    [ "$(id -u)" -eq 0 ] || fail "run this as root (the Unraid terminal is root)"
}

need_iptables() {
    local tool
    for tool in iptables iptables-restore sha256sum; do
        command -v "${tool}" >/dev/null 2>&1 || fail "${tool} is not installed"
    done
    chain_exists DOCKER-USER || fail "the chain DOCKER-USER does not exist. Is Docker running, with its iptables rules (not \"iptables\": false, not the nftables firewall backend)? Nothing was changed."
}

# --- who can change what root runs (the rule of the other root-run scripts of deploy/unraid) --------
loose_modes() {
    local -a paths=("${SELF}" "${HERE}")
    [ ! -e "${STATE_FILE}" ] || paths+=("${STATE_FILE}")
    [ -z "${ENV_FILE}" ] || [ ! -e "${ENV_FILE}" ] || paths+=("${ENV_FILE}")
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
            line="permissions of ${top} were loose and have been tightened (chmod -R go-w; were: ${found//${nl}/, }); if you did not expect this, check 'git status' there"
            warn "${line}"
            logger -t mcpanel -- "instance firewall: ${line}" 2>/dev/null || true
            return 0
        fi
    else
        while read -r _ line; do printf -v repair '%s %q' "${repair}" "${line}"; done <<<"${found}"
    fi
    found="    ${found//${nl}/${nl}    }"
    if [ "$1" = refuse ]; then
        logger -t mcpanel -- "instance firewall NOT changed: group or others can write to what root runs here (repair as root: ${repair})" 2>/dev/null || true
        fail "refusing: group or others can write to what root runs here (mode, path):
${found}
Whoever can change these files could run code as root. Nothing was changed. Repair it (as root),
then run this command again:
    ${repair}"
    fi
    warn "group or others can write to what root runs here (mode, path):
${found}
'install' and 'remove' run as root repair it themselves in a checkout; or now (as root):
    ${repair}"
}

# --- commands ---------------------------------------------------------------------------------------
describe() {
    local entry
    say "Game containers' addresses: ${SOURCES[*]} (pool ${POOL}; shared network ${NETWORK})"
    say "Dropped: new connections from them to this host and to ${PRIVATE[*]}"
    if [ "${#DNS[@]}" -gt 0 ]; then say "DNS allowed to: ${DNS[*]}"; else say "DNS allowed to: (no LAN resolver in ${RESOLV})"; fi
    if [ "${#ALLOWS[@]}" -gt 0 ]; then say "Also allowed (--allow): ${ALLOWS[*]}"; fi
    for entry in "${NOTES[@]+"${NOTES[@]}"}"; do say "Note: ${entry}"; done
    if [ "${#IPV6[@]}" -gt 0 ]; then
        say "IPv6 is enabled on ${IPV6[*]}: these rules are IPv4 only. IPv6 connections of those containers are NOT restricted."
    else
        say "IPv6: none of these networks has IPv6 (the rules are IPv4 only)."
    fi
}

# save_state yes|no: the stored entries and INSTALLED=, which deploy/unraid/array-start.sh reads at boot
# (it puts the rules back only where the owner had installed them)
save_state() {
    local entry
    (
        umask 077
        {
            say "# instance-firewall.sh: what every install repeats (written by install; --forget ENTRY removes one)"
            say "INSTALLED=$1"
            for entry in "${ALLOWS[@]+"${ALLOWS[@]}"}"; do say "ALLOW=${entry}"; done
            for entry in "${STORED_DNS[@]+"${STORED_DNS[@]}"}"; do say "ALLOW_DNS=${entry}"; done
        } >"${STATE_FILE}.new" && mv -f "${STATE_FILE}.new" "${STATE_FILE}"
    ) || fail "could not write ${STATE_FILE}"
}

# mark_removed: INSTALLED=no in the state file, the stored entries kept (array-start.sh no longer puts the
# rules back at boot). Nothing to do without a state file: nothing was installed with this version.
mark_removed() {
    local line
    [ -f "${STATE_FILE}" ] || return 0
    (
        umask 077
        while IFS= read -r line || [ -n "${line}" ]; do
            case "${line}" in INSTALLED=*) ;; *) say "${line}" ;; esac
        done <"${STATE_FILE}" >"${STATE_FILE}.new" || exit 1
        say "INSTALLED=no" >>"${STATE_FILE}.new" || exit 1
        mv -f "${STATE_FILE}.new" "${STATE_FILE}"
    ) || fail "could not write ${STATE_FILE}"
}

# wait_for_docker: up to WAIT seconds for DOCKER-USER and, when docker answers, the shared network
wait_for_docker() {
    local waited=0 name
    name="${NETWORK:-$(env_value AGENT_INSTANCE_NETWORK "${ENV_FILE}")}"
    name="${name:-${DEFAULT_NETWORK}}"
    while [ "${waited}" -lt "${WAIT}" ]; do
        if chain_exists DOCKER-USER; then
            command -v docker >/dev/null 2>&1 || return 0
            docker network inspect "${name}" >/dev/null 2>&1 && return 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
    return 0
}

cmd_install() {
    local applied
    need_root
    check_modes refuse
    [ "${WAIT}" = 0 ] || wait_for_docker
    read_config
    need_iptables
    restore_text | iptables-restore -n || fail "iptables-restore refused the rules; nothing was changed"
    applied="$(snapshot_hash)"
    chain_exists "${MARK}A-${applied}" || iptables -N "${MARK}A-${applied}" || fail "could not record the rules"
    drop_markers A "${applied}"
    drop_markers C "$(config_hash)"
    ensure_jump DOCKER-USER "${FWD}"
    ensure_jump INPUT "${IN}"
    save_state yes
    describe
    say "Installed: chains ${FWD} (from DOCKER-USER) and ${IN} (from INPUT). They are gone after a reboot until this runs again."
    logger -t mcpanel -- "instance firewall installed for ${SOURCES[*]} (config $(config_hash))" 2>/dev/null || true
}

cmd_remove() {
    need_root
    check_modes refuse
    command -v iptables >/dev/null 2>&1 || fail "iptables is not installed"
    local chain removed=0
    while iptables -D DOCKER-USER -j "${FWD}" 2>/dev/null; do removed=1; done
    while iptables -D INPUT -j "${IN}" 2>/dev/null; do removed=1; done
    for chain in "${FWD}" "${IN}"; do
        if chain_exists "${chain}"; then
            iptables -F "${chain}" || fail "could not empty the chain ${chain}"
            iptables -X "${chain}" || fail "could not delete the chain ${chain}"
            removed=1
        fi
    done
    [ -z "$(markers C)$(markers A)" ] || removed=1
    drop_markers C
    drop_markers A
    mark_removed
    if [ "${removed}" = 1 ]; then
        say "Removed: the game containers can reach this host and the LAN again."
        logger -t mcpanel -- "instance firewall removed" 2>/dev/null || true
    else
        say "Nothing to remove: the rules are not installed."
    fi
    say "If a User Scripts entry installs them at array start, take that out as well."
}

cmd_status() {
    local problems=() want applied
    check_modes warn
    read_config
    command -v iptables >/dev/null 2>&1 || fail "iptables is not installed"
    [ "$(id -u)" -eq 0 ] || fail "run this as root: reading the firewall needs it"
    describe
    if ! chain_exists "${FWD}" && ! chain_exists "${IN}" && [ "$(jumps DOCKER-USER "${FWD}")" = 0 ] &&
        [ "$(jumps INPUT "${IN}")" = 0 ] && [ -z "$(markers C)$(markers A)" ]; then
        if [ -f "${STATE_FILE}" ] && grep -qx 'INSTALLED=yes' "${STATE_FILE}"; then
            say "Instance firewall: missing, although it was installed (INSTALLED=yes): the boot tasks put it back, or now: bash ${SELF} install"
            return 1
        fi
        say "Instance firewall: not installed (a normal state; to install it: bash ${SELF} install)"
        return 3
    fi
    chain_exists "${FWD}" || problems+=("the chain ${FWD} is missing")
    chain_exists "${IN}" || problems+=("the chain ${IN} is missing")
    [ "$(jumps DOCKER-USER "${FWD}")" -ge 1 ] || problems+=("DOCKER-USER does not jump to ${FWD}")
    [ "$(jumps INPUT "${IN}")" -ge 1 ] || problems+=("INPUT does not jump to ${IN}")
    applied="$(markers A)"
    if [ -z "${applied}" ] || [ "${applied}" != "$(snapshot_hash)" ]; then
        problems+=("the rules were changed after install")
    fi
    want="$(config_hash)"
    [ "$(markers C)" = "${want}" ] || problems+=("the settings changed since install (pool, network subnet, DNS or --allow)")
    if [ "${#problems[@]}" -gt 0 ]; then
        say "Instance firewall: differs: $(printf '%s; ' "${problems[@]}")run: bash ${SELF} install"
        return 1
    fi
    say "Instance firewall: installed and up to date (config ${want})."
    return 0
}

cmd_dry_run() {
    check_modes warn
    read_config
    describe
    say ""
    say "install would run (as root):"
    say "iptables-restore -n <<'RULES'"
    restore_text
    say "RULES"
    say "iptables -N ${MARK}A-<fingerprint of the rules as loaded>   (and the older marker chains deleted)"
    say "iptables -C DOCKER-USER -j ${FWD} 2>/dev/null || iptables -I DOCKER-USER 1 -j ${FWD}"
    say "iptables -C INPUT -j ${IN} 2>/dev/null || iptables -I INPUT 1 -j ${IN}"
    if [ "${#ADD_ALLOW[@]}" -gt 0 ] || [ "${#ADD_DNS[@]}" -gt 0 ] || [ "${#FORGET[@]}" -gt 0 ]; then
        say "and store in ${STATE_FILE}: ${ALLOWS[*]:-} ${STORED_DNS[*]:-}"
    fi
    say "Nothing was changed."
}

action="${1:-}"
[ "$#" -gt 0 ] && shift
STORED_ALLOWS=()
STORED_DNS=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --allow)
            [ "$#" -ge 2 ] || fail "--allow needs CIDR[:PORT]"
            entry="$(allow_entry "$2")" || fail "--allow: not an IPv4 address or network with an optional port: '$2'"
            ADD_ALLOW+=("${entry}")
            shift 2
            ;;
        --allow-dns)
            [ "$#" -ge 2 ] || fail "--allow-dns needs an IPv4 address"
            entry="$(cidr4 "$2")" || fail "--allow-dns: not an IPv4 address: '$2'"
            [ "${entry#*/}" = 32 ] || fail "--allow-dns: not an IPv4 address: '$2'"
            ADD_DNS+=("${entry%/32}")
            shift 2
            ;;
        --forget)
            [ "$#" -ge 2 ] || fail "--forget needs an entry"
            entry="$(allow_entry "$2")" || fail "--forget: not an entry: '$2'"
            FORGET+=("${entry}" "${entry%/32}")
            shift 2
            ;;
        --env)
            [ "$#" -ge 2 ] || fail "--env needs a file"
            ENV_FILE="$2"
            shift 2
            ;;
        --network)
            [ "$#" -ge 2 ] || fail "--network needs a name"
            NETWORK="$2"
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
    install) cmd_install ;;
    status) cmd_status ;;
    dry-run) cmd_dry_run ;;
    remove)
        [ "${#ADD_ALLOW[@]}${#ADD_DNS[@]}${#FORGET[@]}" = 000 ] || usage
        cmd_remove
        ;;
    *) usage ;;
esac
