"""deploy/unraid: the stopping_docker hook and its installer, run with stand-ins for docker and logger."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

UNRAID_DIR = Path(__file__).resolve().parents[2] / "deploy" / "unraid"
HOOK = UNRAID_DIR / "stopping_docker"
INSTALLER = UNRAID_DIR / "install-hook.sh"

pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("timeout") and HOOK.exists()),
    reason="needs bash, coreutils timeout and the deploy/ folder of the repository",
)

# Records every call; answers like the real CLI for the few subcommands the hook uses.
FAKE_DOCKER = r"""#!/bin/bash
printf 'docker %s\n' "$*" >>"${STUB_LOG}"
case "${1:-}" in
    version)
        [ "${STUB_DOCKER_DOWN:-}" = 1 ] && exit 1
        echo "27.5.1"
        ;;
    ps)
        case "$*" in
            *com.docker.compose.service=panel*) [ -n "${STUB_PANEL:-}" ] && echo "${STUB_PANEL}" ;;
            *io.mcpanel.managed=true*) [ -n "${STUB_SERVERS:-}" ] && printf '%b' "${STUB_SERVERS}" ;;
        esac
        ;;
    exec)
        sleep "${STUB_EXEC_SLEEP:-0}"
        if [ -n "${STUB_EXEC_OUTPUT:-}" ]; then
            printf '%b' "${STUB_EXEC_OUTPUT}" >&2
        else
            echo "stopped   Alpha in 3 s"
            echo "host_shutdown: node 'local': 1 stopped, 0 failed"
        fi
        exit "${STUB_EXEC_RC:-0}"
        ;;
    stop)
        [ "${4:-}" = "${STUB_STOP_FAILS:-none}" ] && exit 1
        ;;
esac
exit 0
"""

FAKE_LOGGER = r"""#!/bin/bash
# logger -t mcpanel -- <message>
shift 3
printf 'LOG %s\n' "$*" >>"${STUB_LOG}"
"""

TWO_SERVERS = "aaa111 mcpanel-aaaaaaaaaaaa\\nbbb222 mcpanel-bbbbbbbbbbbb\\n"
# What Django prints (on stderr, exit status 1) for a command the image does not have: a release
# from before host_shutdown, e.g. after a rollback of the images while the hook stays installed.
UNKNOWN_COMMAND = "Unknown command: 'host_shutdown'\\nType 'manage.py help' for usage.\\n"
LEFT_TO_DOCKER = "2 running server(s) are left to Docker's own stop"


@pytest.fixture
def run_hook(tmp_path: Path) -> Callable[..., tuple[int, list[str]]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("logger", FAKE_LOGGER)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    log = tmp_path / "calls.log"

    def _run(hook: Path = HOOK, **stub_env: str) -> tuple[int, list[str]]:
        log.write_text("")
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_LOG": str(log), **stub_env}
        # the hook must only ever reach the stand-in, never a real Docker on the machine running the tests
        assert shutil.which("docker", path=env["PATH"]) == str(bin_dir / "docker")
        done = subprocess.run(["bash", str(hook), "stopping_docker"], env=env, capture_output=True, text=True, timeout=60)
        assert done.stderr == ""
        return done.returncode, log.read_text().splitlines()

    return _run


def with_limits(tmp_path: Path, **limits: int) -> Path:
    """A copy of the hook with shorter time limits (the real ones would make these tests take minutes)."""
    text = HOOK.read_text()
    for name, value in limits.items():
        text, count = re.subn(rf"^{name}=\d+", f"{name}={value}", text, flags=re.MULTILINE)
        assert count == 1, name
    copy = tmp_path / "stopping_docker"
    copy.write_text(text)
    copy.chmod(0o755)
    return copy


def stops(lines: list[str]) -> list[str]:
    return sorted(line for line in lines if line.startswith("docker stop"))


def logged(lines: list[str]) -> str:
    return "\n".join(line[4:] for line in lines if line.startswith("LOG "))


def test_both_scripts_parse_and_are_executable():
    for script in (HOOK, INSTALLER):
        assert script.read_text().startswith("#!/bin/bash\n")
        assert os.access(script, os.X_OK), script
        assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0


def test_the_panel_stops_the_servers(run_hook):
    rc, lines = run_hook(STUB_PANEL="panel123", STUB_SERVERS=TWO_SERVERS)
    assert rc == 0
    assert "docker exec panel123 python manage.py host_shutdown --timeout 45" in lines
    panel_lookup = next(line for line in lines if line.startswith("docker ps -q"))
    assert "label=com.docker.compose.project=mcpanel" in panel_lookup
    assert "label=com.docker.compose.service=panel" in panel_lookup
    assert stops(lines) == []  # no docker stop behind the panel's back
    text = logged(lines)
    assert "stopped   Alpha in 3 s" in text  # the command's own lines reach the syslog
    assert "finished after" in text and "fallback" not in text


def test_fallback_when_the_panel_is_not_running(run_hook):
    """The only case with a plain docker stop: nobody else gives the servers time to save."""
    rc, lines = run_hook(STUB_SERVERS=TWO_SERVERS)
    assert rc == 0
    assert not any(line.startswith("docker exec") for line in lines)
    assert stops(lines) == ["docker stop -t 45 aaa111", "docker stop -t 45 bbb222"]
    text = logged(lines)
    assert "is not running" in text
    assert "fallback: docker stop -t 45 for 2 running server(s), without the panel" in text
    assert "stay stopped after the next boot" in text  # nobody marked them for a restart
    assert "fallback: stopped mcpanel-aaaaaaaaaaaa" in text and "fallback: stopped mcpanel-bbbbbbbbbbbb" in text


def test_fallback_reports_a_container_it_could_not_stop(run_hook):
    rc, lines = run_hook(STUB_SERVERS=TWO_SERVERS, STUB_STOP_FAILS="bbb222")
    assert rc == 0  # always: the hook must never block or fail the shutdown
    text = logged(lines)
    assert "fallback: stopped mcpanel-aaaaaaaaaaaa" in text
    assert "fallback: could not stop mcpanel-bbbbbbbbbbbb" in text


def test_no_docker_stop_when_the_panel_does_not_know_the_command(run_hook):
    """After a rollback to images from before host_shutdown, with the hook still installed: a
    docker stop would count as a manual stop, nothing would mark the servers, and they would stay
    stopped after the next boot. Left alone, Docker stops them and starts them again at boot."""
    rc, lines = run_hook(STUB_PANEL="panel123", STUB_SERVERS=TWO_SERVERS, STUB_EXEC_RC="1", STUB_EXEC_OUTPUT=UNKNOWN_COMMAND)
    assert rc == 0
    assert any(line.startswith("docker exec panel123 python manage.py host_shutdown") for line in lines)
    assert stops(lines) == []
    text = logged(lines)
    assert "Unknown command: 'host_shutdown'" in text  # the panel's own words reach the syslog
    assert "this panel has no host_shutdown command" in text
    assert "install-hook.sh remove" in text
    assert LEFT_TO_DOCKER in text and "Docker starts them again at the next boot" in text
    assert "fallback" not in text and "the panel command failed" not in text


def test_no_docker_stop_when_the_panel_command_fails(run_hook):
    """The panel runs but could not do it (its agent or its database does not answer): the same.
    Servers it did mark are started by it after boot, the others by Docker."""
    rc, lines = run_hook(STUB_PANEL="panel123", STUB_SERVERS=TWO_SERVERS, STUB_EXEC_RC="1")
    assert rc == 0
    assert any(line.startswith("docker exec panel123") for line in lines)
    assert stops(lines) == []
    text = logged(lines)
    assert "the panel command failed (exit 1)" in text
    assert LEFT_TO_DOCKER in text and "Docker starts them again at the next boot" in text
    assert "fallback" not in text and "no host_shutdown command" not in text


def test_panel_command_that_fails_after_everything_stopped(run_hook):
    rc, lines = run_hook(STUB_PANEL="panel123", STUB_EXEC_RC="1")
    assert rc == 0 and stops(lines) == []
    assert "no Minecraft server container is running any more" in logged(lines)


def test_nothing_to_stop(run_hook):
    rc, lines = run_hook()
    assert rc == 0 and stops(lines) == []
    assert "no Minecraft server container is running" in logged(lines)


def test_docker_that_does_not_answer(run_hook):
    rc, lines = run_hook(STUB_PANEL="panel123", STUB_SERVERS=TWO_SERVERS, STUB_DOCKER_DOWN="1")
    assert rc == 0
    assert [line for line in lines if line.startswith("docker")] == ["docker version --format {{.Server.Version}}"]
    assert "Docker does not answer" in logged(lines)


def test_another_compose_project_name(run_hook):
    _rc, lines = run_hook(STUB_PANEL="panel123", MCPANEL_COMPOSE_PROJECT="minecraft")
    assert any("label=com.docker.compose.project=minecraft" in line for line in lines)


def test_a_hanging_panel_command_is_cut_off(run_hook, tmp_path):
    hook = with_limits(tmp_path, COMMAND_LIMIT=2, BUDGET=30)
    rc, lines = run_hook(hook, STUB_PANEL="panel123", STUB_SERVERS=TWO_SERVERS, STUB_EXEC_SLEEP="30")
    assert rc == 0
    text = logged(lines)
    assert "the panel command failed (exit 124)" in text
    # the panel may have marked and stopped some of them already: no docker stop for the rest
    assert stops(lines) == [] and LEFT_TO_DOCKER in text


def test_fallback_uses_the_time_that_is_left(run_hook, tmp_path):
    hook = with_limits(tmp_path, BUDGET=30)
    rc, lines = run_hook(hook, STUB_SERVERS=TWO_SERVERS)
    assert rc == 0
    # 30 s for the whole hook, minus 3 s in reserve (and what the lookups took)
    found = stops(lines)
    assert len(found) == 2 and all(re.fullmatch(r"docker stop -t 2[567] (aaa111|bbb222)", line) for line in found)


def test_no_docker_stop_when_too_little_time_is_left(run_hook, tmp_path):
    hook = with_limits(tmp_path, BUDGET=10)
    rc, lines = run_hook(hook, STUB_SERVERS=TWO_SERVERS)
    assert rc == 0 and stops(lines) == []
    assert "no time left to stop 2 running server(s)" in logged(lines)


def test_the_watchdog_ends_a_hook_that_hangs(run_hook, tmp_path):
    hook = with_limits(tmp_path, HARD_LIMIT=2)
    rc, lines = run_hook(hook, STUB_PANEL="panel123", STUB_SERVERS=TWO_SERVERS, STUB_EXEC_SLEEP="30")
    assert rc == 0
    assert "the hook was ended by its watchdog after 2s" in logged(lines)
    assert stops(lines) == []


# --- install-hook.sh --------------------------------------------------------------------------------
def installer(root: Path, *args: str, disk_cfg: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin", "MCPANEL_HOOK_ROOT": str(root)}
    # never the real /boot/config/disk.cfg of the machine that runs the tests
    env["MCPANEL_DISK_CFG"] = str(disk_cfg if disk_cfg is not None else root / "no-disk.cfg")
    return subprocess.run(["bash", str(INSTALLER), *args], env=env, capture_output=True, text=True, timeout=30)


def test_install_status_remove(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    target = root / "mcpanel" / "event" / "stopping_docker"

    assert installer(root, "status").returncode == 1  # not installed
    done = installer(root, "install")
    assert done.returncode == 0 and str(target) in done.stdout
    assert target.read_bytes() == HOOK.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o755
    assert installer(root, "status").returncode == 0
    assert installer(root, "install").returncode == 0  # again at the next boot: no error

    target.write_text(target.read_text() + "# older\n")
    stale = installer(root, "status")
    assert stale.returncode == 1 and "differs" in stale.stdout
    target.chmod(0o644)
    assert "not executable" in installer(root, "status").stdout

    assert installer(root, "remove").returncode == 0
    assert not (root / "mcpanel").exists()  # the folders it made are gone too
    assert installer(root, "remove").returncode == 0  # nothing left: still fine
    assert installer(root, "status").returncode == 1


def test_remove_keeps_a_folder_with_other_files(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    assert installer(root, "install").returncode == 0
    other = root / "mcpanel" / "event" / "started"
    other.write_text("#!/bin/bash\n")
    assert installer(root, "remove").returncode == 0
    assert other.exists() and not (root / "mcpanel" / "event" / "stopping_docker").exists()


def test_installer_refuses_a_host_that_is_not_unraid_and_unknown_commands(tmp_path):
    missing = installer(tmp_path / "nowhere", "install")
    assert missing.returncode == 1 and "does not look like an Unraid server" in missing.stderr
    for args in (("bogus",), (), ("status", "extra"), ("install", "--bogus")):
        usage = installer(tmp_path, *args)
        assert usage.returncode == 64 and "install [--project <name>] | status | remove" in usage.stdout, args


# --- install --project: the Compose project of the panel stack ---------------------------------------
@pytest.mark.parametrize("args", [("--project", "minecraft_2"), ("--project=minecraft_2",)])
def test_install_writes_the_compose_project_into_the_installed_hook(tmp_path, run_hook, args):
    """Unraid passes no environment to the hook, so MCPANEL_COMPOSE_PROJECT cannot be set for it."""
    root = tmp_path / "plugins"
    root.mkdir()
    target = root / "mcpanel" / "event" / "stopping_docker"
    done = installer(root, "install", *args)
    assert done.returncode == 0, done.stderr
    assert "Compose project 'minecraft_2'" in done.stdout
    # the installed copy is the hook with exactly that one line changed
    ours, theirs = HOOK.read_text().splitlines(), target.read_text().splitlines()
    changed = [(a, b) for a, b in zip(ours, theirs, strict=True) if a != b]
    assert changed == [('PROJECT="${MCPANEL_COMPOSE_PROJECT:-mcpanel}"', 'PROJECT="${MCPANEL_COMPOSE_PROJECT:-minecraft_2}"')]
    assert target.stat().st_mode & 0o777 == 0o755

    # and it looks for the panel of that project, with no environment variable set
    _rc, lines = run_hook(target, STUB_PANEL="panel123")
    panel_lookup = next(line for line in lines if line.startswith("docker ps -q"))
    assert "label=com.docker.compose.project=minecraft_2" in panel_lookup

    status = installer(root, "status")
    assert status.returncode == 0 and "Installed and up to date" in status.stdout
    assert "Compose project 'minecraft_2'" in status.stdout
    target.write_text(target.read_text() + "# older\n")
    stale = installer(root, "status")
    assert stale.returncode == 1 and "install --project minecraft_2" in stale.stdout  # how to update it

    # a later install without the option goes back to the default
    assert installer(root, "install").returncode == 0
    assert target.read_bytes() == HOOK.read_bytes()
    assert "Compose project 'mcpanel'" in installer(root, "status").stdout


@pytest.mark.parametrize("name", ["My Stack", "x;rm -rf /", "$(id)", "a/b", "-x", "", "UPPER", "a" * 64])
def test_install_refuses_names_that_are_no_compose_project(tmp_path, name):
    root = tmp_path / "plugins"
    root.mkdir()
    done = installer(root, "install", "--project", name)
    assert done.returncode == 1 and "not a Compose project name" in done.stderr
    assert not (root / "mcpanel").exists()
    assert installer(root, "install", "--project").returncode == 1  # the name is missing


# --- Unraid's shutdown time-out -----------------------------------------------------------------------
def disk_cfg(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "disk.cfg"
    path.write_bytes(text.encode())
    return path


@pytest.mark.parametrize(
    ("text", "said"),
    [
        (
            'startArray="yes"\r\nshutdownTimeout="90"\r\nspindownDelay="0"\r\n',
            "WARNING: Unraid's shutdown time-out is 90 seconds.",
        ),
        ('shutdownTimeout="149"\n', "WARNING: Unraid's shutdown time-out is 149 seconds."),
        ("shutdownTimeout=120\n", "WARNING: Unraid's shutdown time-out is 120 seconds."),
        ('startArray="yes"\n', "WARNING: no shutdown time-out is set"),  # Unraid then uses 90
    ],
    ids=["unraid-default", "just-below", "unquoted", "not-set"],
)
def test_install_and_status_warn_about_a_short_shutdown_timeout(tmp_path, text, said):
    """The hook may use 60 to 70 s before Docker and the array stop; with 90 s Unraid would force the shutdown."""
    root = tmp_path / "plugins"
    root.mkdir()
    cfg = disk_cfg(tmp_path, text)
    for command in ("install", "status"):
        done = installer(root, command, disk_cfg=cfg)
        assert done.returncode == 0, done.stderr  # a warning, not a failure: the hook is in place
        assert said in done.stdout
        assert "at least 150" in done.stdout and "Shutdown time-out" in done.stdout
        assert "enough for the hook" not in done.stdout


@pytest.mark.parametrize("value", ["150", "300"])
def test_no_warning_when_the_shutdown_timeout_is_long_enough(tmp_path, value):
    root = tmp_path / "plugins"
    root.mkdir()
    cfg = disk_cfg(tmp_path, f'shutdownTimeout="{value}"\n')
    for command in ("install", "status"):
        done = installer(root, command, disk_cfg=cfg)
        assert done.returncode == 0
        assert f"Unraid's shutdown time-out is {value} seconds: enough for the hook." in done.stdout
        assert "WARNING" not in done.stdout


def test_a_host_without_disk_cfg_gets_a_note(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    done = installer(root, "install")
    assert done.returncode == 0
    assert "could not be read" in done.stdout and "at least 150 seconds" in done.stdout
    assert "WARNING" not in done.stdout
