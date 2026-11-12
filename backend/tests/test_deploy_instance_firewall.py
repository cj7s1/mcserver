"""deploy/unraid/instance-firewall.sh: the host firewall for the game containers (security review 2026-11-11, A-2).

The script runs here against stand-ins for `iptables`, `iptables-restore`, `docker`, `id` and `logger`
that keep the chains in files: what it installs, how `status` tells installed / missing / differs, that a
second install changes nothing, what `remove` leaves, the stored --allow entries, the IPv6 notice and the
refusals. The same rule set was loaded into a real kernel (iptables-nft 1.8.13 in a container's own network
namespace, 2026-11-12); it has not run on an Unraid host.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = Path(os.environ.get("MCPANEL_FIREWALL_SCRIPT") or REPO / "deploy" / "unraid" / "instance-firewall.sh")

pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("sha256sum") and shutil.which("find")), reason="needs bash, coreutils"
)

FAKE_IPTABLES = r"""#!/bin/bash
# iptables stand-in: one file per chain in $FW/chains, each line a rule as `iptables -S` prints it
C="$FW/chains"
printf 'iptables %s\n' "$*" >>"$FW/calls.log"
builtin() { case "$1" in INPUT | FORWARD | OUTPUT) return 0 ;; esac; return 1; }
case "$1" in
    -S)
        if [ $# -eq 1 ]; then
            for b in INPUT FORWARD OUTPUT; do echo "-P $b ACCEPT"; done
            for f in "$C"/*; do n=${f##*/}; builtin "$n" || echo "-N $n"; done
            for f in "$C"/*; do cat "$f"; done
            exit 0
        fi
        [ -e "$C/$2" ] || { echo "iptables: No chain/target/match by that name." >&2; exit 1; }
        if builtin "$2"; then echo "-P $2 ACCEPT"; else echo "-N $2"; fi
        cat "$C/$2" ;;
    -N) [ ! -e "$C/$2" ] || exit 1; : >"$C/$2" ;;
    -X) [ -e "$C/$2" ] && [ ! -s "$C/$2" ] || exit 1
        grep -q -- "-j $2\$" "$C"/* && exit 1
        rm "$C/$2" ;;
    -F) [ -e "$C/$2" ] || exit 1; : >"$C/$2" ;;
    -I) chain=$2; shift 3; [ -e "$C/$chain" ] || exit 1
        { echo "-A $chain $*"; cat "$C/$chain"; } >"$C/$chain.new"; mv "$C/$chain.new" "$C/$chain" ;;
    -A) chain=$2; shift 2; [ -e "$C/$chain" ] || exit 1; echo "-A $chain $*" >>"$C/$chain" ;;
    -D) chain=$2; shift 2; line="-A $chain $*"; [ -e "$C/$chain" ] || exit 1
        grep -qxF -- "$line" "$C/$chain" || exit 1
        found=0
        while IFS= read -r l; do
            if [ $found = 0 ] && [ "$l" = "$line" ]; then found=1; continue; fi
            printf '%s\n' "$l"
        done <"$C/$chain" >"$C/$chain.new"
        mv "$C/$chain.new" "$C/$chain" ;;
    -C) chain=$2; shift 2; grep -qxF -- "-A $chain $*" "$C/$chain" 2>/dev/null ;;
    *) echo "fake iptables: unhandled $*" >&2; exit 97 ;;
esac
"""

FAKE_RESTORE = r"""#!/bin/bash
# iptables-restore --noflush stand-in: all or nothing, like the real one
printf 'iptables-restore %s\n' "$*" >>"$FW/calls.log"
[ "$1" = -n ] || { echo "fake: only --noflush" >&2; exit 97; }
[ -e "$FW/restore_fails" ] && { echo "iptables-restore: line 7 failed" >&2; exit 1; }
work=$(mktemp -d "$FW/restore.XXXXXX"); cp -a "$FW/chains/." "$work/"
cat >"$FW/restore.last"
committed=0
while IFS= read -r line; do
    case "$line" in
        '*filter') ;;
        COMMIT) committed=1 ;;
        :*' - [0:0]') name=${line#:}; name=${name%% *}; [ -e "$work/$name" ] || : >"$work/$name" ;;
        '-F '*) [ -e "$work/${line#-F }" ] || exit 2; : >"$work/${line#-F }" ;;
        '-A '*) chain=${line#-A }; chain=${chain%% *}; [ -e "$work/$chain" ] || exit 2; echo "$line" >>"$work/$chain" ;;
        *) echo "fake iptables-restore: bad line: $line" >&2; rm -rf "$work"; exit 2 ;;
    esac
done <"$FW/restore.last"
[ "$committed" = 1 ] || { rm -rf "$work"; exit 2; }
rm -rf "$FW/chains"; mv "$work" "$FW/chains"
"""

FAKE_DOCKER = r"""#!/bin/bash
# docker stand-in: $FW/network = "<subnets> |<ipv6>" of the shared network; $FW/managed/<id> = "<name>|<ipv6>"
case "$1 $2" in
    "network inspect")
        name=${*: -1}
        case "$name" in
            mcpanel_instances | "$(cat "$FW/network_name" 2>/dev/null)") [ -e "$FW/network" ] && cat "$FW/network" && exit 0; exit 1 ;;
            *) [ -e "$FW/managed/$name" ] && cat "$FW/managed/$name" && exit 0; exit 1 ;;
        esac ;;
    "network ls") ls "$FW/managed" 2>/dev/null; exit 0 ;;
esac
exit 1
"""

FAKE_ID = '#!/bin/sh\nif [ "$1" = -u ]; then cat "$FW/uid"; else PATH=/usr/bin:/bin exec id "$@"; fi\n'
FAKE_LOGGER = '#!/bin/sh\nprintf "%s\\n" "$*" >>"$FW/logger.log"\n'

POOL = "10.213.0.0/16"
SHARED = "172.19.0.0/16"
PRIVATE = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10")


class Host:
    """A checkout with the script, deploy/.env, and the stand-ins' world."""

    def __init__(self, tmp_path: Path) -> None:
        self.top = tmp_path / "mcpanel-src"
        self.unraid = self.top / "deploy" / "unraid"
        self.unraid.mkdir(parents=True)
        (self.top / ".git").mkdir()
        self.script = self.unraid / "instance-firewall.sh"
        shutil.copy(SCRIPT, self.script)
        self.script.chmod(0o755)
        self.env_file = self.top / "deploy" / ".env"
        self.env_file.write_text(f"MCPANEL_SECRET_KEY=x\nAGENT_INSTANCE_POOL={POOL}\n")
        self.env_file.chmod(0o600)
        self.state = self.top / "deploy" / "instance-firewall.env"
        self.fw = tmp_path / "fw"
        (self.fw / "chains").mkdir(parents=True)
        (self.fw / "chains" / "INPUT").write_text("-A INPUT -p tcp -m tcp --dport 22 -j ACCEPT\n")
        (self.fw / "chains" / "FORWARD").write_text("-A FORWARD -j DOCKER-USER\n")
        (self.fw / "chains" / "DOCKER-USER").write_text("-A DOCKER-USER -j RETURN\n")
        (self.fw / "network").write_text(f"{SHARED} |false\n")
        (self.fw / "uid").write_text("0\n")
        self.resolv = tmp_path / "resolv.conf"
        self.resolv.write_text("nameserver 192.168.1.1\nnameserver 8.8.8.8\nnameserver 127.0.0.53\n")
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, text in (
            ("iptables", FAKE_IPTABLES),
            ("iptables-restore", FAKE_RESTORE),
            ("docker", FAKE_DOCKER),
            ("id", FAKE_ID),
            ("logger", FAKE_LOGGER),
        ):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "FW": str(self.fw), "MCPANEL_FIREWALL_RESOLV": str(self.resolv)}
        return subprocess.run(["bash", str(self.script), *args], env=env, capture_output=True, text=True, timeout=60)

    def chain(self, name: str) -> list[str]:
        path = self.fw / "chains" / name
        return path.read_text().splitlines() if path.exists() else []

    def chains(self) -> list[str]:
        return sorted(path.name for path in (self.fw / "chains").iterdir())

    def calls(self) -> list[str]:
        path = self.fw / "calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def snapshot(self) -> dict[str, list[str]]:
        return {name: self.chain(name) for name in self.chains()}


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


def last(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout.strip().splitlines() or [""])[-1]


def test_install_status_install_again_remove(host):
    missing = host.run("status")
    # not installed: a normal state, its own exit status (3), not a failure
    assert missing.returncode == 3 and last(missing).startswith("Instance firewall: not installed (a normal state")

    done = host.run("install")
    assert done.returncode == 0, done.stderr
    fwd, inp = host.chain("MCPANEL-FWD"), host.chain("MCPANEL-IN")
    assert fwd[0] == "-A MCPANEL-FWD -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN"
    assert inp[0] == "-A MCPANEL-IN -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN"
    for source in (POOL, SHARED):
        # the agent and its game container on the same network; Docker still separates two bridges
        assert f"-A MCPANEL-FWD -s {source} -d {source} -j RETURN" in fwd
        for proto in ("udp", "tcp"):  # the LAN resolver of resolv.conf, not 8.8.8.8 nor 127.0.0.53
            rule = f"-s {source} -d 192.168.1.1/32 -p {proto} -m {proto} --dport 53 -j RETURN"
            assert f"-A MCPANEL-FWD {rule}" in fwd and f"-A MCPANEL-IN {rule}" in inp
        for private in PRIVATE:
            assert f"-A MCPANEL-FWD -s {source} -d {private} -j DROP" in fwd
        assert inp[-1 if source == SHARED else -2] == f"-A MCPANEL-IN -s {source} -j DROP"
    assert not [rule for rule in fwd + inp if "8.8.8.8" in rule or "127.0.0.53" in rule]
    # every exception comes before the drops, and nothing else is dropped (the internet stays open)
    first_drop = next(n for n, rule in enumerate(fwd) if rule.endswith("-j DROP"))
    assert all(rule.endswith("-j DROP") for rule in fwd[first_drop:])
    assert {rule.split(" -d ")[1].split(" ")[0] for rule in fwd[first_drop:]} == set(PRIVATE)
    # one jump each, at the top; the host's own rules stay
    assert host.chain("DOCKER-USER") == ["-A DOCKER-USER -j MCPANEL-FWD", "-A DOCKER-USER -j RETURN"]
    assert host.chain("INPUT") == ["-A INPUT -j MCPANEL-IN", "-A INPUT -p tcp -m tcp --dport 22 -j ACCEPT"]
    markers = [name for name in host.chains() if name.startswith(("MCPANEL-C-", "MCPANEL-A-"))]
    assert len(markers) == 2 and all(len(name) == 26 for name in markers)  # iptables allows 28
    assert "instance firewall installed" in (host.fw / "logger.log").read_text()

    status = host.run("status")
    assert status.returncode == 0 and last(status).startswith("Instance firewall: installed and up to date")
    assert f"Game containers' addresses: {POOL} {SHARED}" in status.stdout

    before = host.snapshot()
    assert host.run("install").returncode == 0
    assert host.snapshot() == before  # idempotent: the same rules, still one jump each

    removed = host.run("remove")
    assert removed.returncode == 0 and "Removed" in removed.stdout
    assert host.chains() == ["DOCKER-USER", "FORWARD", "INPUT"]
    assert host.chain("DOCKER-USER") == ["-A DOCKER-USER -j RETURN"]
    assert host.chain("INPUT") == ["-A INPUT -p tcp -m tcp --dport 22 -j ACCEPT"]
    assert last(host.run("status")).startswith("Instance firewall: not installed")
    assert "Nothing to remove" in host.run("remove").stdout


def test_status_says_what_differs_and_install_repairs_it(host):
    assert host.run("install").returncode == 0
    with (host.fw / "chains" / "MCPANEL-FWD").open("a") as rules:
        rules.write("-A MCPANEL-FWD -j ACCEPT\n")  # changed by hand
    changed = host.run("status")
    assert changed.returncode == 1 and "differs: the rules were changed after install" in last(changed)
    assert host.run("install").returncode == 0 and host.run("status").returncode == 0

    (host.fw / "chains" / "INPUT").write_text("-A INPUT -p tcp -m tcp --dport 22 -j ACCEPT\n")  # jump gone
    assert "INPUT does not jump to MCPANEL-IN" in last(host.run("status"))
    assert host.run("install").returncode == 0

    host.env_file.write_text("AGENT_INSTANCE_POOL=10.214.0.0/16\n")  # the pool changed
    settings = host.run("status")
    assert "the settings changed since install" in last(settings)
    assert host.run("install").returncode == 0 and host.run("status").returncode == 0
    assert "-A MCPANEL-IN -s 10.214.0.0/16 -j DROP" in host.chain("MCPANEL-IN")
    assert len([name for name in host.chains() if name.startswith("MCPANEL-C-")]) == 1  # the old marker went

    (host.fw / "network").write_text("172.20.0.0/16 |false\n")  # the shared network was made again
    assert "the settings changed since install" in last(host.run("status"))


def test_allow_entries_are_stored_repeated_and_forgotten(host):
    done = host.run("install", "--allow", "192.168.1.20:5432", "--allow", "192.168.1.30", "--allow-dns", "10.0.0.53")
    assert done.returncode == 0, done.stderr
    assert stat.S_IMODE(host.state.stat().st_mode) == 0o600
    assert "ALLOW=192.168.1.20/32:5432" in host.state.read_text() and "ALLOW_DNS=10.0.0.53" in host.state.read_text()

    def rules() -> list[str]:
        return host.chain("MCPANEL-FWD") + host.chain("MCPANEL-IN")

    for chain in ("MCPANEL-FWD", "MCPANEL-IN"):
        for proto in ("tcp", "udp"):
            assert f"-A {chain} -s {POOL} -d 192.168.1.20/32 -p {proto} -m {proto} --dport 5432 -j RETURN" in rules()
        assert f"-A {chain} -s {POOL} -d 192.168.1.30/32 -j RETURN" in rules()
        assert f"-A {chain} -s {POOL} -d 10.0.0.53/32 -p udp -m udp --dport 53 -j RETURN" in rules()

    # the install at boot (no options) repeats them
    (host.fw / "chains" / "MCPANEL-FWD").write_text("")
    assert host.run("install").returncode == 0
    assert f"-A MCPANEL-FWD -s {POOL} -d 192.168.1.30/32 -j RETURN" in rules()

    assert host.run("install", "--forget", "192.168.1.30", "--forget", "10.0.0.53").returncode == 0
    assert "192.168.1.30" not in " ".join(rules()) and "10.0.0.53" not in " ".join(rules())
    assert "192.168.1.20/32 -p tcp" in " ".join(rules())

    for bad in ("192.168.1.300", "192.168.1.0/33", "10.0.0.1:0", "10.0.0.1:65536", "x", "10.0.0.1/8 -j ACCEPT"):
        refused = host.run("install", "--allow", bad)
        assert refused.returncode == 1 and "--allow" in refused.stderr, bad


def test_dry_run_prints_the_commands_and_changes_nothing(host):
    (host.fw / "uid").write_text("1000\n")  # no root needed
    before = host.snapshot()
    shown = host.run("dry-run", "--allow", "192.168.1.20:5432")
    assert shown.returncode == 0, shown.stderr
    text = shown.stdout
    assert "iptables-restore -n <<'RULES'" in text and "\nCOMMIT\nRULES\n" in text
    assert f"-A MCPANEL-FWD -s {POOL} -d 10.0.0.0/8 -j DROP" in text
    assert "iptables -C DOCKER-USER -j MCPANEL-FWD 2>/dev/null || iptables -I DOCKER-USER 1 -j MCPANEL-FWD" in text
    assert text.rstrip().endswith("Nothing was changed.")
    assert host.snapshot() == before and not host.state.exists()
    changing = [call for call in host.calls() if call.startswith("iptables-restore") or call.split()[1:2] != ["-S"]]
    assert changing == []


def test_refusals_change_nothing(host):
    before = host.snapshot()
    (host.fw / "uid").write_text("1000\n")
    not_root = host.run("install")
    assert not_root.returncode == 1 and "run this as root" in not_root.stderr
    (host.fw / "uid").write_text("0\n")

    (host.fw / "restore_fails").touch()
    refused = host.run("install")
    assert refused.returncode == 1 and "nothing was changed" in refused.stderr
    assert host.snapshot() == before
    (host.fw / "restore_fails").unlink()

    (host.fw / "chains" / "DOCKER-USER").unlink()
    no_docker = host.run("install")
    assert no_docker.returncode == 1 and "DOCKER-USER does not exist" in no_docker.stderr
    assert "MCPANEL-FWD" not in host.chains()

    host.env_file.write_text("AGENT_INSTANCE_POOL=10.213.0.0/16 -j ACCEPT\n")
    bad_pool = host.run("dry-run")
    assert bad_pool.returncode == 1 and "AGENT_INSTANCE_POOL" in bad_pool.stderr
    host.env_file.write_text(f"AGENT_INSTANCE_POOL={POOL}\n")
    bad_net = host.run("dry-run", "--network", "x;y")
    assert bad_net.returncode == 1 and "not a Docker network name" in bad_net.stderr


def test_ipv6_is_named_as_not_restricted(host):
    (host.fw / "network").write_text(f"{SHARED} fd00:dead::/64 |true\n")
    (host.fw / "managed").mkdir()
    (host.fw / "managed" / "abc123").write_text("zz_mcpanel_i_0123456789ab|true\n")
    (host.fw / "managed" / "def456").write_text("zz_mcpanel_i_ba9876543210|false\n")
    shown = host.run("dry-run")
    assert (
        "IPv6 is enabled on mcpanel_instances zz_mcpanel_i_0123456789ab: these rules are IPv4 only. "
        "IPv6 connections of those containers are NOT restricted." in shown.stdout
    )
    assert "fd00" not in shown.stdout.split("RULES")[1]
    (host.fw / "network").write_text(f"{SHARED} |false\n")
    (host.fw / "managed" / "abc123").write_text("zz_mcpanel_i_0123456789ab|false\n")
    assert "IPv6: none of these networks has IPv6" in host.run("dry-run").stdout


def test_without_the_shared_network_only_the_pool_is_covered_and_said_so(host):
    (host.fw / "network").unlink()
    done = host.run("install")
    assert done.returncode == 0
    assert "the shared network mcpanel_instances does not exist (yet): only the pool is covered" in done.stdout
    assert not [rule for rule in host.chain("MCPANEL-FWD") if SHARED in rule]
    # a node: its own env file, another network name
    node_env = host.top / "deploy" / "node.env"
    node_env.write_text("AGENT_INSTANCE_POOL=10.99.0.0/16\nAGENT_INSTANCE_NETWORK=node_instances\n")
    node_env.chmod(0o600)
    (host.fw / "network_name").write_text("node_instances")
    (host.fw / "network").write_text("172.31.0.0/16 |false\n")
    shown = host.run("dry-run", "--env", str(node_env))
    assert "Game containers' addresses: 10.99.0.0/16 172.31.0.0/16" in shown.stdout


def test_as_root_a_loose_checkout_is_tightened_status_only_warns(host):
    host.script.chmod(0o777)
    (host.top / "deploy" / "docker-compose.yml").write_text("name: mcpanel\n")
    (host.top / "deploy" / "docker-compose.yml").chmod(0o666)
    status = host.run("status")
    assert "group or others can write" in status.stderr and stat.S_IMODE(host.script.stat().st_mode) == 0o777
    done = host.run("install")
    assert done.returncode == 0, done.stderr
    assert "were loose and have been tightened" in done.stderr
    assert stat.S_IMODE(host.script.stat().st_mode) == 0o755
    assert stat.S_IMODE((host.top / "deploy" / "docker-compose.yml").stat().st_mode) == 0o644


def test_the_script_says_it_is_untested_on_unraid_and_how_to_check():
    text = SCRIPT.read_text()
    head = text.split("set -u", 1)[0]
    assert "RAN ON ONE REAL UNRAID 7.3.4 SERVER ON 2026-11-12" in head and "NOT SEEN THERE YET" in head
    assert "a container on a per-server network (the pool range)" in " ".join(head.replace("#", " ").split())
    assert "curl -m 5 https://example.com/" in head and "must FAIL" in head and "must WORK" in head
    assert os.access(SCRIPT, os.X_OK)
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0


def test_install_at_boot_waits_for_docker(host):
    """At array start Docker may not have made DOCKER-USER yet: --wait polls for it (every 5 s)."""
    started = time.monotonic()
    assert host.run("install", "--wait", "300").returncode == 0  # everything there: no wait
    assert time.monotonic() - started < 5
    assert host.run("remove").returncode == 0
    (host.fw / "chains" / "DOCKER-USER").unlink()
    started = time.monotonic()
    late = host.run("install", "--wait", "5")
    assert late.returncode == 1 and "DOCKER-USER does not exist" in late.stderr
    assert time.monotonic() - started >= 5
    assert host.run("install", "--wait", "soon").returncode == 1


def test_install_and_remove_are_remembered_for_the_boot(host):
    """deploy/unraid/array-start.sh puts the rules back at boot only where the owner had installed them."""
    assert host.run("remove").returncode == 0
    assert not host.state.exists()  # never installed: nothing remembered
    assert host.run("install", "--allow", "192.168.1.30").returncode == 0
    assert "INSTALLED=yes" in host.state.read_text().splitlines()
    assert stat.S_IMODE(host.state.stat().st_mode) == 0o600
    assert host.run("remove").returncode == 0
    lines = host.state.read_text().splitlines()
    assert "INSTALLED=no" in lines and "INSTALLED=yes" not in lines
    assert "ALLOW=192.168.1.30/32" in lines  # the stored entries stay
    assert host.run("install").returncode == 0
    assert "INSTALLED=yes" in host.state.read_text().splitlines()
    assert f"-A MCPANEL-FWD -s {POOL} -d 192.168.1.30/32 -j RETURN" in host.chain("MCPANEL-FWD")
