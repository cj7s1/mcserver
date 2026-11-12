"""deploy/unraid/first-run.sh: the plugin's first-run checklist (docs/UNRAID.md, "The Unraid plugin") as a program.

The script runs the plugin's real runner (deploy/unraid/plugin/src/bin/mcpanel) against a folder that stands for /
(MCPANEL_PLUGIN_ROOT), with stand-ins for docker, id, logger, curl, ip and iptables, a real git checkout whose deploy
scripts are stand-ins, and a server set up like the owner's after installing the plugin: settings empty, Compose
linked by hand to his own binary, no shutdown hook, /usr/local/lib/docker{,/cli-plugins} mode 777.
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
SCRIPT = REPO / "deploy" / "unraid" / "first-run.sh"
PLUGIN = REPO / "deploy" / "unraid" / "plugin"
SRC = PLUGIN / "src"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "git", "flock", "setsid", "timeout", "sha256sum")),
    reason="needs bash, git, flock, setsid and coreutils",
)

STAND_IN = r"""#!/bin/bash
printf '%s|%s %s\n' "$PWD" "{name}" "$*" >>"$FAKE/calls.log"
{body}
"""
ARRAY_START = r"""case "$1" in
    run)
        if [ -e "$FAKE/array_start_fails" ]; then echo "NEEDS YOU: the stand-in says so"; exit 1; fi
        hook="$MCPANEL_PLUGIN_ROOT/usr/local/emhttp/plugins/mcpanel/event/stopping_docker"
        mkdir -p "$(dirname "$hook")" && printf '#!/bin/bash\n' >"$hook" && chmod 755 "$hook"
        logger -t mcpanel -- "array-start: Shutdown hook: installed"
        echo "Done: everything is in place." ;;
    *) echo "array-start.sh $1 (stand-in)" ;;
esac
"""
FIREWALL = r"""state="$(dirname "$0")/../instance-firewall.env"
case "$1" in
    install) printf 'INSTALLED=yes\n' >"$state"; : >"$FAKE/firewall_on"; echo "Installed: chains (stand-in)" ;;
    remove) printf 'INSTALLED=no\n' >"$state"; rm -f "$FAKE/firewall_on"; echo "Removed (stand-in)" ;;
    *) echo "Instance firewall: $1 (stand-in)" ;;
esac
"""
FAKE_DOCKER = r"""#!/bin/bash
F=$FAKE
printf '%s\n' "$*" >>"$F/docker.log"
case "$1" in
    version) echo 29.8.2 ;;
    compose)
        if [ "$2" = version ] && [ -x "$CLI/docker-compose" ]; then echo "Docker Compose version v5.6.1"; exit 0; fi
        exit 1 ;;
    ps)
        if [[ " $* " == *" --format "* ]]; then cat "$F/panels" 2>/dev/null
        elif [[ "$*" == *com.docker.compose.service=panel* ]]; then echo p1
        else cut -d'|' -f1 "$F/containers" 2>/dev/null; fi ;;
    inspect)
        line=$(grep "^${*: -1}|" "$F/containers") || exit 1
        IFS='|' read -r _ service running nets name oneoff <<<"$line"
        printf '%s|%s|%s|/%s|%s\n' "$service" "$running" "$nets" "$name" "$oneoff" ;;
    port) echo 0.0.0.0:8080 ;;
    network) case "$2" in ls) echo mcpanel_instances ;; inspect) exit 0 ;; *) exit 1 ;; esac ;;
    image) cat "$F/images" 2>/dev/null ;;
    pull) echo "$3" >>"$F/images"; echo "docker.io/library/$3" ;;
    run)
        name=""
        args=("$@")
        for i in "${!args[@]}"; do [ "${args[$i]}" != --name ] || name=${args[$((i + 1))]}; done
        echo "$name" >>"$F/selftests"
        for t in "$@"; do
            [[ "$t" =~ ^[A-Za-z0-9.-]+:[0-9]+$ ]] || continue
            host=${t%:*}
            case "$host" in
                192.168.* | 10.*) [ -e "$F/firewall_on" ] && echo "$t closed" || echo "$t open" ;;
                *) [ -e "$F/firewall_on" ] && [ -e "$F/firewall_blocks_internet" ] && echo "$t closed" || echo "$t open" ;;
            esac
        done ;;
    rm) echo "${*: -1}" >>"$F/removed" ;;
    *) echo "fake docker: unhandled $*" >&2; exit 97 ;;
esac
"""
FAKE_LOGGER = r"""#!/bin/bash
tag=user
while [ "$#" -gt 0 ]; do
    case "$1" in -t) tag=$2; shift 2 ;; --) shift; break ;; *) break ;; esac
done
printf '%s tower %s: %s\n' "$(date '+%b %d %H:%M:%S')" "$tag" "$*" >>"$MCPANEL_PLUGIN_ROOT/var/log/syslog"
"""
FAKE_ID = '#!/bin/sh\nif [ "$1" = -u ]; then cat "$FAKE/uid"; else PATH=/usr/bin:/bin exec id "$@"; fi\n'
FAKE_CURL = '#!/bin/sh\nprintf "%s\\n" "$*" >>"$FAKE/curl.log"\nexit "$(cat "$FAKE/curl_rc" 2>/dev/null || echo 0)"\n'
FAKE_IP = r"""#!/bin/sh
case "$*" in
    *"route get"*) echo "1.1.1.1 via 192.168.1.1 dev br0 src 192.168.1.10 uid 0" ;;
    *"route show default"*) echo "default via 192.168.1.1 dev br0 metric 1" ;;
esac
"""
FAKE_IPTABLES = '#!/bin/sh\n[ -e "$FAKE/firewall_on" ]\n'


def sh(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    done = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


class Server:
    """An Unraid server right after the plugin was installed, like the owner's on 2026-11-12."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.root = tmp / "root"
        for sub in ("tmp", "run", "boot/config/plugins", "usr/local/lib", "var/log"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "var/log/syslog").write_text("")
        (self.root / "boot/config/ident.cfg").write_text('USE_SSL="no"\nPORT="80"\nPORTSSL="443"\n')
        self.plugin = self.root / "usr/local/emhttp/plugins/mcpanel"
        for name in ("bin/mcpanel", "event/docker_started"):
            (self.plugin / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(SRC / name, self.plugin / name)
            (self.plugin / name).chmod(0o755)
        shutil.copy(PLUGIN / "mcpanel.plg", self.root / "boot/config/plugins/mcpanel.plg")
        (self.root / "boot/config/plugins/mcpanel").mkdir()  # there and empty, as on his server
        self.cfg = self.root / "boot/config/plugins/mcpanel/mcpanel.cfg"
        self.fake = tmp / "fake"
        self.fake.mkdir()
        (self.fake / "uid").write_text("0\n")
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for name, text in (
            ("docker", FAKE_DOCKER),
            ("id", FAKE_ID),
            ("logger", FAKE_LOGGER),
            ("curl", FAKE_CURL),
            ("ip", FAKE_IP),
            ("iptables", FAKE_IPTABLES),
        ):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        # his own Compose binary, linked by hand into the CLI plugin folder made with root's umask 000
        self.compose = tmp / "cache/appdata/mcpanel-ops/bin/docker-compose"
        self.compose.parent.mkdir(parents=True)
        self.compose.write_text("#!/bin/sh\necho compose\n")
        self.compose.chmod(0o700)
        self.compose.parent.chmod(0o700)
        self.cli = self.root / "usr/local/lib/docker/cli-plugins"
        self.cli.mkdir(parents=True)
        (self.cli / "docker-compose").symlink_to(self.compose)
        self.cli.parent.chmod(0o777)
        self.cli.chmod(0o777)
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "FAKE": str(self.fake),
            "CLI": str(self.cli),
            "MCPANEL_PLUGIN_ROOT": str(self.root),
            "HOME": str(tmp),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "LC_ALL": "C",
        }
        self.checkout = self.make_checkout()
        (self.fake / "panels").write_text(f"mcpanel|{self.checkout}/deploy\n")
        (self.fake / "containers").write_text(
            "p1|panel|true|1|mcpanel-panel-1|\n"
            "a1|agent|true|2|mcpanel-agent-1|\n"
            "d1|postgres|true|1|mcpanel-postgres-1|\n"
            "o1|panel|false|1|mcpanel-panel-run-0123|True\n"
        )
        (self.fake / "images").write_text("alpine:3\nitzg/minecraft-server:stable\n")

    def make_checkout(self) -> Path:
        origin = self.tmp / "origin.git"
        sh("git", "init", "-q", "--bare", "-b", "main", str(origin), env=self.env)
        top = self.tmp / "appdata" / "mcpanel-src"
        (top / "deploy/scripts").mkdir(parents=True)
        (top / "deploy/unraid/plugin").mkdir(parents=True)
        bodies = {
            "scripts/upgrade.sh": 'echo "upgrade.sh $1 (stand-in)"\n',
            "unraid/array-start.sh": ARRAY_START,
            "unraid/install-hook.sh": "",
            "unraid/instance-firewall.sh": FIREWALL,
            "unraid/stopping_docker": "",
        }
        for rel, body in bodies.items():
            path = top / "deploy" / rel
            path.write_text(STAND_IN.replace("{name}", rel.split("/")[-1]).replace("{body}", body))
            path.chmod(0o755)
        (top / "deploy/docker-compose.yml").write_text("services: {}\n")
        shutil.copy(PLUGIN / "mcpanel.plg", top / "deploy/unraid/plugin/mcpanel.plg")
        (top / ".gitignore").write_text("deploy/*.env\n")
        sh("git", "init", "-q", "-b", "main", cwd=top, env=self.env)
        sh("git", "add", "-A", cwd=top, env=self.env)
        sh("git", "commit", "-q", "-m", "first", cwd=top, env=self.env)
        sh("git", "remote", "add", "origin", str(origin), cwd=top, env=self.env)
        sh("git", "push", "-q", "-u", "origin", "main", cwd=top, env=self.env)
        sh("chmod", "-R", "go-w", str(top))
        return top

    def run(self, *args: str, answers: str = "") -> subprocess.CompletedProcess[str]:
        # a copy of the script elsewhere: it finds everything through the installed plugin
        copy = self.tmp / "first-run.sh"
        shutil.copy(SCRIPT, copy)
        return subprocess.run(
            ["bash", str(copy), *args], env=self.env, input=answers, capture_output=True, text=True, timeout=300
        )

    def snapshot(self) -> dict[str, tuple]:
        seen: dict[str, tuple] = {}
        for base in (self.root, self.checkout):
            for path in sorted(base.rglob("*")):
                info = path.lstat()
                link = os.readlink(path) if path.is_symlink() else ""
                seen[str(path)] = (stat.S_IMODE(info.st_mode), info.st_size, info.st_mtime_ns, link)
        return seen


@pytest.fixture
def server(tmp_path: Path) -> Server:
    return Server(tmp_path)


def state_of(out: str, label: str) -> str:
    """The state word(s) of a checklist line ("OK", "TO DO", ...)."""
    for line in out.splitlines():
        rest = line[12:]
        if rest.startswith(label + " ") or rest.rstrip() == label:
            return line[:11].strip()
    raise AssertionError(f"no line {label!r} in:\n{out}")


def test_status_of_a_fresh_install_like_the_owners_and_it_writes_nothing(server):
    before = server.snapshot()
    done = server.run()  # status is the default
    assert done.returncode == 0, done.stdout + done.stderr
    out = done.stdout
    assert state_of(out, "Plugin") == "OK"
    assert state_of(out, "Checkout") == "OK" and f"{server.checkout} (detected)" in out
    assert state_of(out, "Compose binary") == "TO DO"
    assert f"linked by hand to {server.compose}: not set in the plugin, gone after the next reboot" in out
    assert state_of(out, "Docker Compose") == "OK"
    assert state_of(out, "Shutdown hook") == "TO DO"
    assert state_of(out, "Docker CLI folders") == "TO DO" and "777" in out
    assert state_of(out, "Last boot run") == "TO DO"
    assert state_of(out, "Firewall") == "OK" and "not installed (optional" in out
    assert out.rstrip().splitlines()[-1].startswith("Next: bash first-run.sh run")
    assert server.snapshot() == before, "status changed something"


def test_dry_run_lists_the_commands_and_writes_nothing(server):
    before = server.snapshot()
    done = server.run("dry-run")
    assert done.returncode == 0, done.stdout + done.stderr
    out = done.stdout
    assert f"/usr/local/emhttp/plugins/mcpanel/bin/mcpanel save '' '' {server.compose}" in out
    assert f"cd {server.checkout}/deploy && bash scripts/upgrade.sh status" in out
    assert "fetch origin main" in out
    assert f"bash {server.checkout}/deploy/unraid/array-start.sh dry-run --project mcpanel" in out
    assert f"bash {server.checkout}/deploy/unraid/instance-firewall.sh status" in out
    assert f"bash {server.checkout}/deploy/unraid/array-start.sh run --project mcpanel" in out
    assert "Nothing was changed. Next: bash first-run.sh run" in out
    assert server.snapshot() == before, "dry-run changed something"


def test_run_yes_takes_a_fresh_install_to_all_ok_and_a_second_run_changes_nothing(server):
    done = server.run("run", "--yes")
    assert done.returncode == 0, done.stdout + done.stderr
    out = done.stdout
    for said in (
        f"Docker's Compose link points to {server.compose}, which the plugin does not know",
        "Save these settings in the plugin? [y/N] yes (--yes)",
        "Run the boot tasks now (what the plugin does at every Docker start)? [y/N] yes (--yes)",
        "Steps 1 to 4 are done.",
        "Next: reboot when it suits you, then: bash first-run.sh after-reboot",
    ):
        assert said in out, said
    assert f'COMPOSE_BIN="{server.compose}"' in server.cfg.read_text()
    assert (server.plugin / "event/stopping_docker").exists()
    assert stat.S_IMODE(server.cli.stat().st_mode) == stat.S_IMODE(server.cli.parent.stat().st_mode) == 0o755
    logs = sorted(p.name.split("-", 2)[2] for p in (server.root / "run/mcpanel/logs").glob("*.log"))
    assert logs == ["boot-dry-run.log", "boot-run.log", "check-updates.log", "firewall-status.log", "status.log"]
    assert "  | MCPanel plugin " in out  # every task's log is shown
    status = server.run()
    assert status.returncode == 0
    assert "TO DO" not in status.stdout and "NEEDS YOU" not in status.stdout
    assert "Next: bash first-run.sh after-reboot" in status.stdout
    before = server.snapshot()
    again = server.run("run", "--yes")
    assert again.returncode == 0, again.stdout
    assert "shown already" in again.stdout and "ran ok at" in again.stdout and "Save these settings" not in again.stdout
    assert server.snapshot() == before, "a second run changed something"


def test_run_asks_and_a_no_stops_without_a_change(server):
    before = server.snapshot()
    done = server.run("run", answers="n\n")
    assert done.returncode == 1
    assert "Save these settings in the plugin? [y/N]" in done.stdout
    assert "STOPPED: the settings were not saved." in done.stdout
    assert server.snapshot() == before


def test_a_checkout_others_can_change_stops_the_run_with_the_repair(server):
    (server.checkout / "deploy/unraid/array-start.sh").chmod(0o777)
    status = server.run()
    assert state_of(status.stdout, "Checkout") == "NEEDS YOU"
    done = server.run("run", "--yes")
    assert done.returncode == 1
    assert f"chown -R root:root {server.checkout} && chmod -R go-w {server.checkout}" in done.stdout
    assert done.stdout.rstrip().splitlines()[-1].startswith("STOPPED: group or others can write")
    assert not (server.plugin / "event/stopping_docker").exists()


def test_a_boot_run_that_needs_you_stops_with_what_it_said(server):
    (server.fake / "array_start_fails").touch()
    done = server.run("run", "--yes")
    assert done.returncode == 1
    assert "STOPPED: array-start.sh reported something to do" in done.stdout
    assert "NEEDS YOU: the stand-in says so" in done.stdout  # the log, shown


def test_given_settings_win_and_a_missing_checkout_is_named(server):
    (server.fake / "panels").write_text("")  # no running stack to find it from
    status = server.run()
    assert state_of(status.stdout, "Checkout") == "NEEDS YOU"
    assert "run --checkout /mnt/user/appdata/mcpanel-src" in status.stdout
    done = server.run("run", "--yes", "--checkout", str(server.checkout), "--project", "mcpanel")
    assert done.returncode == 0, done.stdout
    assert f'CHECKOUT="{server.checkout}"' in server.cfg.read_text()


def test_after_reboot_says_whether_the_boot_tasks_ran_at_this_boot(server):
    assert server.run("run", "--yes").returncode == 0
    time.sleep(2.2)
    (server.root / "proc").mkdir()
    (server.root / "proc/uptime").write_text("1.00 1.00\n")  # booted a second ago, after the run above
    before = server.run("after-reboot")
    assert state_of(before.stdout, "Boot tasks") == "TO DO" and "no run since this boot" in before.stdout
    (server.root / "proc/uptime").write_text("600.00 600.00\n")
    done = server.run("after-reboot")
    assert done.returncode == 0
    out = done.stdout
    assert state_of(out, "Boot tasks") == "OK" and "ran ok at this boot" in out
    assert state_of(out, "Syslog") == "OK" and "mcpanel: plugin: boot tasks finished: ok" in out
    assert state_of(out, "Shutdown hook") == "OK"
    for name in ("mcpanel-panel-1", "mcpanel-agent-1", "mcpanel-postgres-1"):
        assert state_of(out, name) == "OK"
    assert "mcpanel-panel-run" not in out  # a one-off is not part of the stack
    assert "Step 6 (when it suits you)" in out


def test_firewall_install_self_test_passes_and_is_kept(server):
    assert server.run("run", "--yes").returncode == 0
    done = server.run("firewall", "--yes")
    assert done.returncode == 0, done.stdout + done.stderr
    out = done.stdout
    assert "NOT RUN ON A REAL UNRAID SERVER YET" in out
    for said in (
        "PASS        must fail              192.168.1.10:80 (this server's web UI): blocked",
        "PASS        must fail              192.168.1.1:80 (a private address on your LAN): blocked",
        "PASS        must work              1.1.1.1:443 (the internet): reachable",
        "PASS        must work              github.com:443 (the internet, by name: DNS works): reachable",
        "Keep the firewall (it is put back at every boot)? [y/N] yes (--yes)",
    ):
        assert said in out, said
    assert (server.checkout / "deploy/instance-firewall.env").read_text() == "INSTALLED=yes\n"
    assert (server.fake / "selftests").read_text().split() == ["mcpanel-firstrun-selftest"] * 2
    assert "mcpanel-firstrun-selftest" in (server.fake / "removed").read_text()  # removed afterwards
    assert state_of(server.run().stdout, "Firewall") == "OK"


def test_firewall_that_cuts_the_internet_is_removed_again(server):
    assert server.run("run", "--yes").returncode == 0
    (server.fake / "firewall_blocks_internet").touch()
    done = server.run("firewall", "--yes")
    assert done.returncode == 1
    assert "FAIL        must work              1.1.1.1:443: NOT reachable" in done.stdout
    assert "taking it out again" in done.stdout
    assert (server.checkout / "deploy/instance-firewall.env").read_text() == "INSTALLED=no\n"


def test_firewall_not_kept_when_the_owner_says_no(server):
    assert server.run("run", "--yes").returncode == 0
    done = server.run("firewall", answers="y\nn\n")
    assert done.returncode == 0, done.stdout
    assert "Keep the firewall" in done.stdout and "Removed (stand-in)" in done.stdout
    assert (server.checkout / "deploy/instance-firewall.env").read_text() == "INSTALLED=no\n"


def test_firewall_asks_before_pulling_an_image_and_test_and_remove_modes(server):
    assert server.run("run", "--yes").returncode == 0
    (server.fake / "images").write_text("itzg/minecraft-server:stable\n")
    refused = server.run("firewall", "test", answers="n\n")
    assert refused.returncode == 1 and "Download alpine:3 (about 4 MB) for it? [y/N]" in refused.stdout
    assert "pull" not in (server.fake / "docker.log").read_text().split()
    test = server.run("firewall", "test", "--yes")
    assert test.returncode == 0, test.stdout
    assert "pull -q alpine:3" in (server.fake / "docker.log").read_text()
    # without the firewall, a game server reaches this server and the LAN: the self-test says so
    assert "FAIL        must fail              192.168.1.10:80: still reachable from a game server" in test.stdout
    assert server.run("firewall", "--yes").returncode == 0
    removed = server.run("firewall", "remove", "--yes")
    assert removed.returncode == 0 and "Removed (stand-in)" in removed.stdout
    assert (server.checkout / "deploy/instance-firewall.env").read_text() == "INSTALLED=no\n"


def test_root_the_plugin_and_the_arguments(server):
    (server.fake / "uid").write_text("1000\n")
    assert "run this as root" in server.run().stdout
    (server.fake / "uid").write_text("0\n")
    for bad in (("frobnicate",), ("run", "--nope"), ("status", "--checkout", "/x"), ("firewall", "wipe")):
        assert server.run(*bad).returncode == 64, bad
    assert "paths must be absolute" in server.run("run", "--compose-bin", "relative").stdout
    shutil.rmtree(server.plugin)
    missing = server.run()
    assert missing.returncode == 1 and "the MCPanel plugin is not installed" in missing.stdout


def test_the_script_says_what_has_run_where_and_holds_no_personal_details():
    text = SCRIPT.read_text()
    head = text.split("set -u", 1)[0]
    assert "NOT RUN ON A REAL UNRAID SERVER YET" in head
    assert "It does nothing of its own" in head
    for forbidden in ("192.168.", "cj7s1", "/mnt/cache/appdata", "eval "):
        assert forbidden not in text, forbidden
    assert os.access(SCRIPT, os.X_OK)
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_the_docs_and_ci_name_the_script():
    unraid = " ".join((REPO / "docs/UNRAID.md").read_text().split())
    for needle in (
        "Or let the script walk you through it",
        "first-run.sh dry-run",
        "first-run.sh run",
        "after-reboot",
        "first-run.sh firewall",
        "It changes nothing itself: every change goes through the plugin.",
    ):
        assert needle in unraid, needle
    assert "shellcheck deploy/unraid/first-run.sh" in (REPO / ".github/workflows/ci.yml").read_text()
    assert "first-run.sh" in (REPO / "CLAUDE.md").read_text() and "first-run.sh" in (REPO / "DEVELOPMENT_STATUS.md").read_text()
