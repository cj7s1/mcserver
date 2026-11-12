"""deploy/unraid/array-start.sh: everything MCPanel needs at an Unraid array start (one User Scripts line).

From the owner's server after a reboot: Unraid ships no Docker Compose (his standalone binary was linked into
/usr/local/lib/docker/cli-plugins, which is in RAM), the panel container restarted for hours because Docker
29 had dropped its network after a failed first start, and the shutdown hook was never installed. The
script runs here against stand-ins for `docker`, `id` and `logger` (a stand-in `docker` that keeps the
platform containers in a file), with the real install-hook.sh and a stand-in instance-firewall.sh.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from .test_upgrade_script import Site
from .test_upgrade_script import pytestmark as _upgrade_skip

REPO = Path(__file__).resolve().parents[2]
UNRAID = Path(os.environ.get("MCPANEL_UNRAID_SCRIPTS") or REPO / "deploy" / "unraid")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# docker stand-in. $FAKE/containers: lines "id|service|running|network mode|networks|name|oneoff" (networks
# space separated); $FAKE/networks: lines "name|compose project|key in the compose file". The answers of
# `docker compose version` come from the plugin link in $CLI (like the real CLI) or $FAKE/compose_builtin;
# $FAKE/daemon_down makes the daemon not answer; $FAKE/connect_fails makes every connect fail; a connect to a
# network the container is on fails like Docker's ("endpoint ... already exists").
FAKE_DOCKER = r"""#!/bin/bash
F=$FAKE
printf '%s\n' "$*" >>"$F/calls.log"
rows() { cat "$F/containers" 2>/dev/null; }
set_row() { # set_row ID FIELD VALUE (fields: 1 id 2 service 3 running 4 mode 5 networks 6 name 7 oneoff)
    local tmp="$F/containers.new"
    while IFS='|' read -r id service running mode nets name oneoff; do
        if [ "$id" = "$1" ]; then
            case "$2" in 3) running=$3 ;; 5) nets=$3 ;; esac
        fi
        printf '%s|%s|%s|%s|%s|%s|%s\n' "$id" "$service" "$running" "$mode" "$nets" "$name" "$oneoff"
    done < <(rows) >"$tmp"
    mv "$tmp" "$F/containers"
}
field() { rows | while IFS='|' read -r id service running mode nets name oneoff; do [ "$id" = "$1" ] && eval "printf '%s' \"\$$2\""; done; }
block() { # block CMD: with $F/block_CMD (seconds) this call waits once, after it marked $F/in_CMD
    [ -e "$F/block_$1" ] || return 0
    : >"$F/in_$1"
    sleep "$(cat "$F/block_$1")"
    rm -f "$F/block_$1"
}
case "$1" in
    compose)
        if [ "$2" = version ] && { [ -x "$CLI/docker-compose" ] || [ -e "$F/compose_builtin" ]; }; then
            echo "Docker Compose version v5.7.0"; exit 0
        fi
        echo "docker: 'compose' is not a docker command." >&2; exit 1 ;;
    version) [ -e "$F/daemon_down" ] && { echo "Cannot connect to the Docker daemon" >&2; exit 1; }; echo 29.9.2 ;;
    ps) rows | cut -d'|' -f1 ;;
    inspect)
        id=${*: -1}
        line=$(rows | grep "^$id|") || exit 1
        IFS='|' read -r _ service running mode nets name oneoff <<<"$line"
        case "$*" in
            *'range $k'*) for n in $nets; do printf '%s ' "$n"; done; echo ;;
            *) set -- $nets; printf '%s|%s|%s|%s|/%s|%s\n' "$service" "$running" "$mode" "$#" "$name" "$oneoff" ;;
        esac ;;
    stop) block stop; set_row "${*: -1}" 3 false ;;
    start)
        block start
        id=${*: -1}
        [ -n "$(field "$id" nets)" ] || { echo "failed to set up container networking" >&2; exit 1; }
        set_row "$id" 3 true ;;
    network)
        case "$2" in
            ls)
                project=""; key=""
                for a in "$@"; do
                    case "$a" in
                        label=com.docker.compose.project=*) project=${a#label=com.docker.compose.project=} ;;
                        label=com.docker.compose.network=*) key=${a#label=com.docker.compose.network=} ;;
                    esac
                done
                while IFS='|' read -r name p k; do
                    [ "$p" = "$project" ] && [ "$k" = "$key" ] && echo "$name"
                done <"$F/networks" ;;
            inspect) cut -d'|' -f1 "$F/networks" | grep -qx "${*: -1}" ;;
            connect)
                id=${*: -1}
                net=${*: -2:1}
                block connect
                [ ! -e "$F/connect_fails" ] || { echo "Error response from daemon: connect refused" >&2; exit 1; }
                nets=$(field "$id" nets)
                case " $nets " in *" $net "*)
                    echo "Error response from daemon: endpoint with name $id already exists in network $net" >&2; exit 1 ;;
                esac
                set_row "$id" 5 "${nets:+$nets }$net" ;;
            *) exit 1 ;;
        esac ;;
    *) echo "fake docker: unhandled $*" >&2; exit 97 ;;
esac
"""
FAKE_ID = '#!/bin/sh\nif [ "$1" = -u ]; then cat "$FAKE/uid"; else PATH=/usr/bin:/bin exec id "$@"; fi\n'
FAKE_LOGGER = '#!/bin/sh\nprintf "%s\\n" "$*" >>"$FAKE/syslog"\n'
FAKE_FIREWALL = """#!/bin/bash
printf '%s\\n' "$*" >>"$FAKE/firewall.log"
case "$1" in
    install) echo "Game containers' addresses: 10.213.0.0/16 172.19.0.0/16"; echo "Installed: chains ..." ;;
    status) echo "Instance firewall: installed and up to date (config 0123456789abcdef)." ;;
esac
"""


INTERNAL = "mcpanel_internal"
BOTH = "mcpanel_internal mcpanel_instances"
NONE = ""


class Box:
    """An Unraid host as far as the script sees it: a checkout, the flash folder, RAM folders, Docker."""

    def __init__(self, tmp_path: Path) -> None:
        self.top = tmp_path / "mcpanel-src"
        self.unraid = self.top / "deploy" / "unraid"
        self.unraid.mkdir(parents=True)
        (self.top / ".git").mkdir()
        for name in ("array-start.sh", "install-hook.sh", "stopping_docker"):
            shutil.copy(UNRAID / name, self.unraid / name)
            (self.unraid / name).chmod(0o755)
        (self.unraid / "instance-firewall.sh").write_text(FAKE_FIREWALL)
        (self.unraid / "instance-firewall.sh").chmod(0o755)
        self.boot = tmp_path / "boot" / "config" / "plugins" / "mcpanel"
        self.cli = tmp_path / "usr-local-lib-docker" / "cli-plugins"
        self.plugins = tmp_path / "emhttp-plugins"
        self.plugins.mkdir()
        self.fake = tmp_path / "fake"
        self.fake.mkdir()
        (self.fake / "uid").write_text("0\n")
        # as Compose makes them: <project>_internal, and instances with the fixed name of the compose file
        (self.fake / "networks").write_text("mcpanel_internal|mcpanel|internal\nmcpanel_instances|mcpanel|instances\n")
        self.store = tmp_path / "cache" / "system"  # where the owner keeps the Compose binary
        self.store.mkdir(parents=True)
        self.compose_bin = self.store / "docker-compose"
        self.compose_bin.write_text("#!/bin/sh\necho compose\n")
        self.compose_bin.chmod(0o755)
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, text in (("docker", FAKE_DOCKER), ("id", FAKE_ID), ("logger", FAKE_LOGGER)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        # the stack as Compose makes it (the owner's server, audit probe/platform.json): the agent's NetworkMode is
        # the instance network, and it is on the internal one too
        self.containers(
            ("p1", "panel", "true", "mcpanel_internal", INTERNAL, "mcpanel-panel-1"),
            ("a1", "agent", "true", "mcpanel_instances", BOTH, "mcpanel-agent-1"),
            ("d1", "postgres", "true", "mcpanel_internal", INTERNAL, "mcpanel-postgres-1"),
        )

    def containers(self, *rows: tuple) -> None:
        """Rows (id, service, running, network mode, networks, name[, oneoff])."""
        (self.fake / "containers").write_text("".join("|".join(map(str, (*row, "")[:7])) + "\n" for row in rows))

    def container(self, cid: str) -> list[str]:
        for line in (self.fake / "containers").read_text().splitlines():
            if line.startswith(cid + "|"):
                return line.split("|")
        raise KeyError(cid)

    def run(self, *args: str, wait_stack: int = 0) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "FAKE": str(self.fake),
            "CLI": str(self.cli),
            "MCPANEL_BOOT_STATE": str(self.boot),
            "MCPANEL_CLI_PLUGINS": str(self.cli),
            "MCPANEL_STACK_WAIT": str(wait_stack),
            "MCPANEL_RUN_LOCK": str(self.fake / "array-start.lock"),
            "MCPANEL_HOOK_ROOT": str(self.plugins),
            "MCPANEL_DISK_CFG": str(self.plugins / "no-disk.cfg"),
        }
        return subprocess.run(
            ["bash", str(self.unraid / "array-start.sh"), *args], env=env, capture_output=True, text=True, timeout=60
        )

    def calls(self, prefix: str = "") -> list[str]:
        path = self.fake / "calls.log"
        lines = path.read_text().splitlines() if path.exists() else []
        return [line for line in lines if line.startswith(prefix)]

    def syslog(self) -> str:
        path = self.fake / "syslog"
        return path.read_text() if path.exists() else ""

    def hook_installed(self) -> bool:
        return (self.plugins / "mcpanel" / "event" / "stopping_docker").exists()


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


CHANGING = ("stop", "start", "network connect")


def test_first_boot_links_compose_installs_the_hook_and_repairs_the_panel(box):
    box.containers(
        ("p1", "panel", "false", "mcpanel_internal", NONE, "mcpanel-panel-1"),  # the restart loop of the owner's server
        ("a1", "agent", "true", "mcpanel_instances", BOTH, "mcpanel-agent-1"),
        ("d1", "postgres", "true", "mcpanel_internal", INTERNAL, "mcpanel-postgres-1"),
    )
    done = box.run("--compose-bin", str(box.compose_bin))
    assert done.returncode == 0, done.stdout + done.stderr
    # 1. Compose: linked into the CLI plugin folder (in RAM on Unraid), the path remembered on the flash
    link = box.cli / "docker-compose"
    assert link.is_symlink() and os.readlink(link) == str(box.compose_bin)
    state = box.boot / "array-start.env"
    assert f"COMPOSE_BIN={box.compose_bin}" in state.read_text().splitlines()
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    # 3. the shutdown hook
    assert box.hook_installed()
    # 4. the firewall was never installed by the owner: not touched
    assert not (box.fake / "firewall.log").exists()
    assert "Firewall: not installed on this host by its owner" in done.stdout
    # 5. the panel: stopped, connected with its service name as alias, started
    assert box.calls("stop") == ["stop -t 30 p1"]
    assert box.calls("network connect") == ["network connect --alias panel mcpanel_internal p1"]
    assert box.calls("start") == ["start p1"]
    assert box.container("p1")[2:5] == ["true", "mcpanel_internal", "mcpanel_internal"]
    for said in (
        "Compose: linked",
        "Shutdown hook: installed",
        "mcpanel-panel-1 is not on mcpanel_internal",
        "repaired and started",
    ):
        assert said in box.syslog(), said
    assert done.stdout.rstrip().endswith("Done: everything is in place.")


def test_later_boots_need_no_argument(box):
    assert box.run("--compose-bin", str(box.compose_bin)).returncode == 0
    shutil.rmtree(box.cli)  # a reboot: /usr/local/lib/docker/cli-plugins is in RAM
    shutil.rmtree(box.plugins / "mcpanel")  # ... and so is Unraid's plugin folder with the hook
    again = box.run()
    assert again.returncode == 0, again.stdout + again.stderr
    assert os.readlink(box.cli / "docker-compose") == str(box.compose_bin) and box.hook_installed()
    assert box.calls("stop") == []  # nothing to repair


def test_without_compose_it_says_how_to_get_one_and_goes_on(box):
    done = box.run()
    assert done.returncode == 1
    assert "Docker Compose is missing (Unraid ships none) and no binary is configured" in done.stdout
    assert "--compose-bin /path/to/docker-compose" in done.stdout
    assert box.hook_installed()  # the other steps ran
    assert "Done, but 1 thing(s) need you:" in done.stdout
    (box.fake / "compose_builtin").touch()  # a host with Compose (a plugin of Unraid's apps)
    ok = box.run()
    assert ok.returncode == 0 and "Compose: ok (Docker Compose version" in ok.stdout and not box.cli.exists()


@pytest.mark.parametrize("loose", ["binary", "folder"])
def test_a_compose_binary_others_can_write_is_not_linked(box, loose):
    (box.compose_bin if loose == "binary" else box.store).chmod(0o777)
    done = box.run("--compose-bin", str(box.compose_bin))
    assert done.returncode == 1 and "not linked: group or others can write" in done.stdout
    assert not (box.cli / "docker-compose").exists()
    missing = box.run("--compose-bin", str(box.store / "nope"))
    assert "no executable file there; not remembered" in missing.stdout


def test_the_firewall_is_put_back_only_where_it_was_installed(box):
    (box.fake / "compose_builtin").touch()
    state = box.top / "deploy" / "instance-firewall.env"
    state.write_text("INSTALLED=no\nALLOW=192.168.1.20/32\n")
    assert box.run().returncode == 0 and not (box.fake / "firewall.log").exists()
    state.write_text("INSTALLED=yes\n")
    done = box.run()
    assert done.returncode == 0
    assert (box.fake / "firewall.log").read_text().splitlines() == ["install --wait 120"]
    assert "Firewall: installed again" in done.stdout


def test_without_docker_only_the_hook_is_installed(box):
    (box.fake / "compose_builtin").touch()
    (box.fake / "daemon_down").touch()
    done = box.run("--wait", "0")
    assert done.returncode == 1 and "the Docker daemon did not answer within 0 s" in done.stdout
    assert box.hook_installed() and box.calls("ps") == []


def test_the_agent_gets_the_internal_network_back_whatever_its_network_mode_says(box):
    # F-4 of the review: Compose gives the agent the instance network as NetworkMode; the panel finds it on internal
    (box.fake / "compose_builtin").touch()
    box.containers(("a1", "agent", "false", "mcpanel_instances", NONE, "mcpanel-agent-1"))
    done = box.run()
    assert done.returncode == 0, done.stdout
    assert box.calls("network connect") == [
        "network connect --alias agent mcpanel_internal a1",
        "network connect --alias agent mcpanel_instances a1",
    ]
    assert box.container("a1")[2:5] == ["true", "mcpanel_instances", BOTH]
    assert "mcpanel-agent-1 repaired and started" in done.stdout


def test_only_what_a_container_misses_is_connected(box):
    (box.fake / "compose_builtin").touch()
    box.containers(("a1", "agent", "true", "mcpanel_instances", "mcpanel_instances", "mcpanel-agent-1"))
    done = box.run()
    assert done.returncode == 0, done.stdout
    assert box.calls("network connect") == ["network connect --alias agent mcpanel_internal a1"]


def test_the_networks_come_from_composes_labels_for_another_project(box):
    (box.fake / "compose_builtin").touch()
    (box.fake / "networks").write_text("games_internal|games|internal\nmcpanel_instances|games|instances\n")
    box.containers(("a1", "agent", "false", "mcpanel_instances", NONE, "games-agent-1"))
    done = box.run("--project", "games")
    assert done.returncode == 0, done.stdout
    assert box.calls("network connect") == [
        "network connect --alias agent games_internal a1",
        "network connect --alias agent mcpanel_instances a1",
    ]


def test_a_repair_by_the_agent_meanwhile_is_left_alone(box):
    # F-7: the agent repaired the panel between this script's look and its stop: the script's connect then fails
    # with "already exists", which is what it wanted; it starts what it stopped
    (box.fake / "compose_builtin").touch()
    box.containers(("p1", "panel", "true", "mcpanel_internal", NONE, "mcpanel-panel-1"))
    real = box.bin / "docker-real"
    (box.bin / "docker").rename(real)
    (box.bin / "docker").write_text(
        "#!/bin/bash\n"
        'R="$(dirname "$0")/docker-real"\n'
        'if [ "$1" = stop ]; then "$R" "$@"; rc=$?; "$R" network connect --alias panel mcpanel_internal "${*: -1}" >/dev/null; exit $rc; fi\n'
        'exec "$R" "$@"\n'
    )
    (box.bin / "docker").chmod(0o755)
    done = box.run()
    assert done.returncode == 0, done.stdout
    assert box.container("p1")[2:5] == ["true", "mcpanel_internal", "mcpanel_internal"]
    assert "mcpanel-panel-1 is on mcpanel_internal already" in done.stdout and "NEEDS YOU" not in done.stdout


def test_a_container_is_looked_at_again_right_before_the_repair(box):
    (box.fake / "compose_builtin").touch()
    box.containers(("p1", "panel", "true", "mcpanel_internal", NONE, "mcpanel-panel-1"))
    (box.bin / "docker").rename(box.bin / "docker-real")
    # the agent repairs the panel after the script's first look and before its second
    (box.bin / "docker").write_text(
        "#!/bin/bash\n"
        'R="$(dirname "$0")/docker-real"\n'
        'case "$*" in *"range"*) echo x >>"$FAKE/looks"; [ "$(grep -c . "$FAKE/looks")" != 2 ] || '
        '"$R" network connect --alias panel mcpanel_internal p1 >/dev/null ;; esac\n'
        'exec "$R" "$@"\n'
    )
    (box.bin / "docker").chmod(0o755)
    done = box.run()
    assert done.returncode == 0, done.stdout
    assert "mcpanel-panel-1 is on its networks again (repaired meanwhile): nothing to do" in done.stdout
    assert box.calls("stop") == [] and box.calls("start") == []


def start_script(box: Box) -> subprocess.Popen[str]:
    env = {
        "PATH": f"{box.bin}:/usr/bin:/bin",
        "FAKE": str(box.fake),
        "CLI": str(box.cli),
        "MCPANEL_BOOT_STATE": str(box.boot),
        "MCPANEL_CLI_PLUGINS": str(box.cli),
        "MCPANEL_STACK_WAIT": "0",
        "MCPANEL_HOOK_ROOT": str(box.plugins),
        "MCPANEL_DISK_CFG": str(box.plugins / "no-disk.cfg"),
    }
    return subprocess.Popen(
        ["bash", str(box.unraid / "array-start.sh")], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def wait_for(path: Path) -> None:
    end = time.monotonic() + 20
    while not path.exists():
        assert time.monotonic() < end, f"{path.name} never came"
        time.sleep(0.02)


def broken_panel(box: Box) -> None:
    (box.fake / "compose_builtin").touch()
    box.containers(("p1", "panel", "true", "mcpanel_internal", NONE, "mcpanel-panel-1"))


@pytest.mark.parametrize("step", ["stop", "connect", "start"])
def test_what_the_script_stops_is_started_again_whenever_it_is_ended(box, step):
    # F-7: a signal (the end of a User Script, a shutdown) while the script's docker stop, connect or start runs:
    # the container is noted before its stop, so the EXIT trap starts it in every case
    broken_panel(box)
    (box.fake / f"block_{step}").write_text("1\n")
    run = start_script(box)
    wait_for(box.fake / f"in_{step}")
    run.terminate()
    run.communicate(timeout=30)
    assert run.returncode == 143
    assert box.calls("stop") == ["stop -t 30 p1"]
    starts = box.calls("start")
    assert starts and set(starts) == {"start p1"}, "the EXIT trap starts what the script stopped"
    if step == "start":
        assert starts == ["start p1", "start p1"]  # its own start, then the trap's: a no-op on a running container
    if step != "stop":  # once connected, the trap's start brings it up on its network
        assert box.container("p1")[2:5] == ["true", "mcpanel_internal", "mcpanel_internal"]


def test_a_second_signal_does_not_cut_the_trap_short(box):
    broken_panel(box)
    (box.fake / "block_connect").write_text("1\n")
    (box.fake / "block_start").write_text("1\n")
    run = start_script(box)
    wait_for(box.fake / "in_connect")
    run.terminate()  # the first signal: the trap starts the panel (its start blocks a moment)
    wait_for(box.fake / "in_start")
    run.terminate()  # a second one while the trap is at work
    run.communicate(timeout=30)
    assert run.returncode == 143
    assert box.calls("start") == ["start p1"]
    assert box.container("p1")[2:5] == ["true", "mcpanel_internal", "mcpanel_internal"]


def test_starting_a_running_container_again_is_harmless(box):
    # what the trap relies on: a start of a container that runs (or was never stopped) succeeds and changes nothing
    broken_panel(box)
    box.containers(("p1", "panel", "true", "mcpanel_internal", INTERNAL, "mcpanel-panel-1"))
    done = subprocess.run(["docker", "start", "p1"], env={"PATH": f"{box.bin}:/usr/bin:/bin", "FAKE": str(box.fake)})
    assert done.returncode == 0 and box.container("p1")[2:5] == ["true", "mcpanel_internal", INTERNAL]
    text = (UNRAID / "array-start.sh").read_text()
    assert text.index('STOPPED+=("${id}")') < text.index('act docker stop -t 30 "${id}"')
    assert "trap '' TERM HUP INT" in text


def test_one_off_containers_are_not_part_of_the_stack(box):
    # F-3: a `docker compose run` leftover (exited) made every boot wait 2 minutes and report "needs you"
    (box.fake / "compose_builtin").touch()
    box.containers(
        ("p1", "panel", "true", "mcpanel_internal", INTERNAL, "mcpanel-panel-1"),
        ("a1", "agent", "true", "mcpanel_instances", BOTH, "mcpanel-agent-1"),
        ("d1", "postgres", "true", "mcpanel_internal", INTERNAL, "mcpanel-postgres-1"),
        ("o1", "panel", "false", "mcpanel_internal", INTERNAL, "mcpanel-panel-run-0123456789ab", "True"),
        ("o2", "panel", "false", "mcpanel_internal", NONE, "mcpanel-panel-run-ba9876543210"),
    )
    began = time.monotonic()
    done = box.run(wait_stack=30)
    assert done.returncode == 0, done.stdout
    assert time.monotonic() - began < 10
    assert "panel, agent and postgres run with their networks" in done.stdout
    assert box.calls("stop") == [] and box.calls("network connect") == []
    status = box.run("status")
    assert "mcpanel-panel-run" not in status.stdout


def test_a_failed_repair_names_the_compose_way(box):
    (box.fake / "compose_builtin").touch()
    box.containers(("p1", "panel", "false", "mcpanel_internal", NONE, "mcpanel-panel-1"))
    (box.fake / "connect_fails").touch()
    done = box.run()
    assert done.returncode == 1 and "docker compose up -d --force-recreate panel" in done.stdout


def test_a_container_that_stays_down_is_reported(box):
    (box.fake / "compose_builtin").touch()
    box.containers(("d1", "postgres", "false", "mcpanel_internal", INTERNAL, "mcpanel-postgres-1"))
    done = box.run()
    assert done.returncode == 1 and "mcpanel-postgres-1 is not running after 0 s" in done.stdout
    assert box.calls("stop") == []  # it has its network: nothing to repair


def test_dry_run_and_status_change_nothing(box):
    box.containers(("p1", "panel", "false", "mcpanel_internal", NONE, "mcpanel-panel-1"))
    (box.fake / "uid").write_text("1000\n")  # neither needs root
    plan = box.run("dry-run", "--compose-bin", str(box.compose_bin))
    assert plan.returncode == 0, plan.stdout + plan.stderr
    text = plan.stdout
    for said in (
        f"would run: ln -sfn {box.compose_bin} {box.cli}/docker-compose",
        f"would remember in {box.boot}/array-start.env: COMPOSE_BIN={box.compose_bin}",
        f"would run: bash {box.unraid}/install-hook.sh install",
        "would run: docker stop -t 30 p1",
        "would run: docker network connect --alias panel mcpanel_internal p1",
        "would run: docker start p1",
        "Nothing was changed.",
    ):
        assert said in text, said
    assert not box.cli.exists() and not box.boot.exists() and not box.hook_installed()
    assert not [call for call in box.calls() if call.startswith(CHANGING)]
    assert box.syslog() == ""

    status = box.run("status")
    assert status.returncode == 1
    for said in (
        "MISSING Compose: 'docker compose' does not answer",
        "MISSING Shutdown hook",
        "REPAIR mcpanel-panel-1: not on mcpanel_internal (network mode mcpanel_internal; 'run' repairs it)",
        f"User Scripts line (At Startup of Array): bash {box.unraid}/array-start.sh",
    ):
        assert said in status.stdout, said
    assert not [call for call in box.calls() if call.startswith(CHANGING)]


def test_status_after_a_good_run_says_ok_everywhere(box):
    assert box.run("--compose-bin", str(box.compose_bin)).returncode == 0
    status = box.run("status")
    assert status.returncode == 0, status.stdout
    lines = [line.strip() for line in status.stdout.splitlines() if line.startswith("  ") and "User Scripts" not in line]
    assert lines and all(line.startswith(("ok", "remembered")) for line in lines), lines


def test_two_runs_wait_for_each_other(box):
    # the plugin's run at Docker start and a User Script left in place: one at a time
    lock = box.fake / "array-start.lock"
    holder = subprocess.Popen(["flock", str(lock), "sleep", "2"])
    try:
        time.sleep(0.3)
        began = time.monotonic()
        done = box.run("--compose-bin", str(box.compose_bin))
        assert done.returncode == 0, done.stdout + done.stderr
        assert time.monotonic() - began >= 1
        assert "another array-start.sh run is at work: waiting for it" in done.stdout
    finally:
        holder.wait(timeout=10)


def test_the_flash_drive_is_written_only_when_a_setting_changes(box):
    assert box.run("--compose-bin", str(box.compose_bin), "--project", "mcpanel").returncode == 0
    state = box.boot / "array-start.env"
    os.utime(state, (1_000_000_000, 1_000_000_000))
    shutil.rmtree(box.cli)
    assert box.run("--compose-bin", str(box.compose_bin), "--project", "mcpanel").returncode == 0
    assert state.stat().st_mtime == 1_000_000_000
    assert box.run("--project", "other").returncode == 0
    assert "PROJECT=other" in state.read_text() and state.stat().st_mtime != 1_000_000_000


def test_status_takes_the_settings_it_is_given_and_names_the_plugin(box):
    assert box.run("--compose-bin", str(box.compose_bin)).returncode == 0
    status = box.run("status", "--project", "mcpanel", "--compose-bin", str(box.compose_bin))
    assert f"remembered ({box.boot}/array-start.env): COMPOSE_BIN={box.compose_bin}" in status.stdout
    assert f"given: --compose-bin {box.compose_bin} --project mcpanel" in status.stdout
    assert "User Scripts line" in status.stdout
    event = box.plugins / "mcpanel" / "event" / "docker_started"
    event.write_text("#!/bin/bash\n")
    event.chmod(0o755)
    status = box.run("status")
    assert "The MCPanel plugin runs 'array-start.sh run' at every Docker start" in status.stdout
    assert "User Scripts line" not in status.stdout


def test_root_and_the_rule_of_the_other_root_scripts(box):
    (box.fake / "uid").write_text("1000\n")
    refused = box.run()
    assert refused.returncode == 1 and "run this as root" in refused.stderr
    (box.fake / "uid").write_text("0\n")
    (box.fake / "compose_builtin").touch()
    (box.unraid / "array-start.sh").chmod(0o777)
    warned = box.run("status")
    assert "WARNING: group or others can write" in warned.stdout
    done = box.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "were loose and have been tightened" in done.stdout
    assert stat.S_IMODE((box.unraid / "array-start.sh").stat().st_mode) == 0o755
    for bad in (("--project", "Bad Name"), ("--compose-bin", "relative/path"), ("--wait", "soon"), ("frobnicate",)):
        assert box.run(*bad).returncode in (1, 64), bad


def test_the_script_says_it_is_untested_on_unraid_and_how_to_check():
    text = (UNRAID / "array-start.sh").read_text()
    head = text.split("set -u", 1)[0]
    assert "RAN ON ONE REAL UNRAID 7.3.4 SERVER ON 2026-11-12" in head and "NOT SEEN THERE YET" in head
    assert "bash array-start.sh dry-run" in head
    assert "bash /mnt/user/appdata/mcpanel-src/deploy/unraid/array-start.sh" in head
    assert os.access(UNRAID / "array-start.sh", os.X_OK)
    assert subprocess.run(["bash", "-n", str(UNRAID / "array-start.sh")], check=False).returncode == 0


@_upgrade_skip
def test_upgrade_sh_names_this_script_when_compose_is_missing(tmp_path: Path):
    site = Site(tmp_path)
    real = site.bin / "docker"
    real.rename(site.bin / "docker-shim")
    real.write_text('#!/bin/bash\n[ "$1 $2" = "compose version" ] && exit 1\nexec "$(dirname "$0")/docker-shim" "$@"\n')
    real.chmod(0o755)
    refused = site.upgrade("status")
    assert refused.returncode == 1 and "the docker compose plugin is missing" in refused.stderr
    assert f"bash {site.deploy}/unraid/array-start.sh --compose-bin" in refused.stderr


def test_the_docs_describe_the_one_line_user_script_and_the_repair():
    def words(path: str) -> str:
        return " ".join((REPO / path).read_text().split())

    line = "bash /mnt/user/appdata/mcpanel-src/deploy/unraid/array-start.sh"
    for path in ("docs/UNRAID.md", "docs/UNRAID-SHUTDOWN.md", "docs/QUICKSTART-UNRAID.md"):
        text = words(path)
        assert line in text and "At Startup of Array" in text, path
        # the two-line form with a separate firewall line is gone
        assert "instance-firewall.sh install --wait 300" not in text, path
    unraid = words("docs/UNRAID.md")
    for needle in (
        "### Without the plugin: the User Script `array-start.sh`",
        "**Not seen at a real boot yet.**",
        "array-start.sh --compose-bin /mnt/cache/system/docker-compose",
        '"failed to resolve host postgres"',
        "docker network connect --alias panel mcpanel_internal mcpanel-panel-1",
    ):
        assert needle in unraid or needle in (REPO / "docs/UNRAID.md").read_text(), needle
    assert "array-start.sh --compose-bin" in words("DEPLOYMENT.md")
    assert "array-start.sh" in words("DEVELOPMENT_STATUS.md") and "array-start.sh" in (REPO / "CLAUDE.md").read_text()
