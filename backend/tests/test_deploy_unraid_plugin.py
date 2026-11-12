"""deploy/unraid/plugin: the Unraid plugin `mcpanel` (feature plan 2026-11-12, section A).

Nothing here is Unraid. The plugin's runner (src/bin/mcpanel) runs against a folder that stands for /
(MCPANEL_PLUGIN_ROOT), stand-ins for `docker`, `id`, `logger` and `curl`, and a real git checkout whose
deploy scripts are stand-ins that write down where and with which arguments they were run. The PHP side is
in test_deploy_unraid_plugin_php.py.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "deploy" / "unraid" / "plugin"
SRC = PLUGIN / "src"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "git", "flock", "setsid", "timeout", "sha256sum")),
    reason="needs bash, git, flock, setsid and coreutils",
)

STAND_IN = r"""#!/bin/bash
# stand-in for deploy/{name}: writes down where and how it was run
printf '%s|%s %s\n' "$PWD" "{name}" "$*" >>"$FAKE/calls.log"
{body}
"""
UPGRADE_BODY = r"""case "$1" in
    status) echo "MCPanel stack \"mcpanel\" (stand-in status)"; [ ! -e "$FAKE/slow" ] || sleep "$(cat "$FAKE/slow")" ;;
    dry-run) echo "  from: abc  to: def (stand-in)"; cat "$FAKE/dry_update" 2>/dev/null || echo "Nothing was changed. To do it: ./scripts/upgrade.sh run" ;;
    run) echo "Done (stand-in)"; echo "Panel tabs that were open during the upgrade: reload them (they still run the old version's code)." ;;
    rollback)
        if [ "${2:-}" = --dry-run ]; then
            cat "$FAKE/dry_rollback" 2>/dev/null || echo "Nothing was changed. To do it: ./scripts/upgrade.sh rollback"
        else echo "rolled back (stand-in)"; fi ;;
esac
exit "$(cat "$FAKE/rc_upgrade" 2>/dev/null || echo 0)"
"""
ARRAY_START_BODY = r"""[ ! -e "$FAKE/slow_boot" ] || sleep "$(cat "$FAKE/slow_boot")"
echo "Done: everything is in place."
"""
FAKE_DOCKER = r"""#!/bin/bash
printf '%s\n' "$*" >>"$FAKE/docker.log"
case "$1" in
    ps)
        if [[ " $* " == *" --format "* ]]; then cat "$FAKE/panels" 2>/dev/null
        elif [ -s "$FAKE/panels" ]; then echo p1; fi ;;
    compose)
        if [ "$2" = version ] && { [ -x "$CLI/docker-compose" ] || [ -e "$FAKE/compose_builtin" ]; }; then
            echo "Docker Compose version v5.7.0"; exit 0
        fi
        exit 1 ;;
    port) cat "$FAKE/port" 2>/dev/null ;;
    *) exit 1 ;;
esac
"""
FAKE_CURL = r"""#!/bin/bash
printf '%s\n' "$*" >>"$FAKE/curl.log"
out=""
while [ "$#" -gt 0 ]; do
    case "$1" in -o) out="$2"; shift ;; esac
    shift
done
[ ! -e "$FAKE/curl_fails" ] || exit 22
cp "$FAKE/payload" "$out"
"""
FAKE_ID = '#!/bin/sh\nif [ "$1" = -u ]; then echo 0; else PATH=/usr/bin:/bin exec id "$@"; fi\n'
FAKE_LOGGER = '#!/bin/sh\nprintf "%s\\n" "$*" >>"$FAKE/syslog"\n'

TASKS = (
    "status check-updates update-prepare update-run rollback-prepare rollback-run boot-status boot-dry-run "
    "boot-run firewall-status firewall-dry-run firewall-install firewall-remove compose-install"
).split()


def sh(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    done = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


class Host:
    """An Unraid server as far as the plugin sees it: the plugin's folder, the flash drive, RAM, a checkout."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.root = tmp / "root"
        for sub in ("tmp", "run", "boot/config/plugins", "usr/local/lib"):
            (self.root / sub).mkdir(parents=True)
        self.plugin = self.root / "usr/local/emhttp/plugins/mcpanel"
        for name in ("bin/mcpanel", "event/docker_started"):
            (self.plugin / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(SRC / name, self.plugin / name)
            (self.plugin / name).chmod(0o755)
        shutil.copy(PLUGIN / "mcpanel.plg", self.root / "boot/config/plugins/mcpanel.plg")
        self.runner = self.plugin / "bin/mcpanel"
        self.cfg = self.root / "boot/config/plugins/mcpanel/mcpanel.cfg"
        self.run_dir = self.root / "run/mcpanel"
        self.cli = self.root / "usr/local/lib/docker/cli-plugins"
        self.fake = tmp / "fake"
        self.fake.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for name, text in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL), ("id", FAKE_ID), ("logger", FAKE_LOGGER)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
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
        self.deploy = self.checkout / "deploy"
        self.unraid = self.deploy / "unraid"

    # --- the checkout and its upstream ------------------------------------------------------------------
    def make_checkout(self) -> Path:
        origin = self.tmp / "origin.git"
        sh("git", "init", "-q", "--bare", "-b", "main", str(origin), env=self.env)
        top = self.tmp / "appdata" / "mcpanel-src"
        (top / "deploy/scripts").mkdir(parents=True)
        (top / "deploy/unraid/plugin").mkdir(parents=True)
        stand_ins = {
            "scripts/upgrade.sh": UPGRADE_BODY,
            "unraid/array-start.sh": ARRAY_START_BODY,
            "unraid/install-hook.sh": "",
            "unraid/instance-firewall.sh": 'echo "Instance firewall: $1 (stand-in)"\nexit "$(cat "$FAKE/fw_rc" 2>/dev/null || echo 0)"\n',
            "unraid/stopping_docker": "",
        }
        for rel, body in stand_ins.items():
            path = top / "deploy" / rel
            path.write_text(STAND_IN.replace("{name}", rel.split("/")[-1]).replace("{body}", body))
            path.chmod(0o755)
        (top / "deploy/docker-compose.yml").write_text("services: {}\n")
        shutil.copy(PLUGIN / "mcpanel.plg", top / "deploy/unraid/plugin/mcpanel.plg")
        (top / ".gitignore").write_text("deploy/upgrade-state.env\n")
        sh("git", "init", "-q", "-b", "main", cwd=top, env=self.env)
        sh("git", "add", "-A", cwd=top, env=self.env)
        sh("git", "commit", "-q", "-m", "first", cwd=top, env=self.env)
        sh("git", "remote", "add", "origin", str(origin), cwd=top, env=self.env)
        sh("git", "push", "-q", "-u", "origin", "main", cwd=top, env=self.env)
        sh("chmod", "-R", "go-w", str(top))
        return top

    def upstream_commit(self, subject: str, plugin_version: str | None = None) -> None:
        other = self.tmp / "other"
        if not other.exists():
            sh("git", "clone", "-q", str(self.tmp / "origin.git"), str(other), env=self.env)
        sh("git", "pull", "-q", cwd=other, env=self.env)
        (other / "README.md").write_text(subject + "\n")
        if plugin_version:
            plg = other / "deploy/unraid/plugin/mcpanel.plg"
            plg.write_text(re.sub(r'version="[^"]+"', f'version="{plugin_version}"', plg.read_text(), count=1))
        sh("git", "add", "-A", cwd=other, env=self.env)
        sh("git", "commit", "-q", "-m", subject, cwd=other, env=self.env)
        sh("git", "push", "-q", cwd=other, env=self.env)

    def head(self) -> str:
        return sh("git", "rev-parse", "HEAD", cwd=self.checkout, env=self.env).strip()

    # --- running the plugin -------------------------------------------------------------------------------
    def run(self, *args: str, runner: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(runner or self.runner), *args], env=self.env, capture_output=True, text=True, timeout=60)

    def facts(self) -> dict[str, str | list[str]]:
        done = self.run("facts")
        assert done.returncode == 0, done.stdout + done.stderr
        facts: dict[str, str | list[str]] = {}
        for line in done.stdout.splitlines():
            key, _, value = line.partition("=")
            if key in ("LOG_FILE", "UPDATE_COMMIT", "USER_SCRIPT"):
                facts.setdefault(key, []).append(value)  # type: ignore[union-attr]
            else:
                facts[key] = value
        return facts

    def lock_free(self) -> bool:
        lock = self.run_dir / "lock"
        if not lock.exists():
            return True
        return subprocess.run(["flock", "-n", str(lock), "true"]).returncode == 0

    def task(self) -> dict[str, str]:
        path = self.run_dir / "task.env"
        return dict(line.split("=", 1) for line in path.read_text().splitlines()) if path.exists() else {}

    def wait(self, seconds: float = 20) -> dict[str, str]:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.lock_free() and self.task().get("RESULT") not in (None, "running"):
                return self.task()
            time.sleep(0.1)
        raise AssertionError(f"the task did not end: {self.task()}")

    def start_and_wait(self, *args: str) -> tuple[dict[str, str], str]:
        done = self.run("start", *args)
        assert done.returncode == 0, done.stdout + done.stderr
        assert done.stdout.startswith("STARTED: "), done.stdout
        task = self.wait()
        return task, Path(task["LOG"]).read_text()

    def calls(self) -> list[str]:
        path = self.fake / "calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def save(self, checkout: str = "", project: str = "", compose_bin: str = "") -> subprocess.CompletedProcess[str]:
        return self.run("save", checkout, project, compose_bin)

    def use_checkout(self) -> None:
        done = self.save(str(self.checkout))
        assert done.returncode == 0, done.stdout


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


def described(host: Host, *args: str) -> list[str]:
    done = host.run("describe", *args)
    lines = done.stdout.splitlines()
    assert lines[0] == "Runs as root with umask 022, in this order:", done.stdout
    return lines[1:]


# --- the build and the .plg ---------------------------------------------------------------------------------
def test_the_build_is_reproducible_and_the_committed_plg_is_a_fresh_build():
    first = sh("bash", str(PLUGIN / "build.sh"), "--stdout")
    second = sh("bash", str(PLUGIN / "build.sh"), "--stdout")
    assert first == second
    assert (PLUGIN / "mcpanel.plg").read_text() == first, "run 'bash deploy/unraid/plugin/build.sh' and commit it"
    assert "mcpanel.plg is up to date" in sh("bash", str(PLUGIN / "build.sh"), "--check")


def test_the_plg_is_well_formed_and_installs_exactly_the_sources():
    plg = ET.parse(PLUGIN / "mcpanel.plg").getroot()
    assert plg.tag == "PLUGIN"
    attrs = plg.attrib
    version = re.search(r"^## (\S+)$", (PLUGIN / "CHANGES.md").read_text(), re.M).group(1)
    assert attrs["name"] == "mcpanel" and attrs["version"] == version
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d{2}[a-z]?", version)
    base = re.search(r'^BASE_URL="([^"]+)"$', (PLUGIN / "build.sh").read_text(), re.M).group(1)
    assert attrs["pluginURL"] == f"{base}/deploy/unraid/plugin/mcpanel.plg"
    assert attrs["min"] == "7.0.0" and attrs["launch"] == "Settings/MCPanel" and attrs["icon"] == "gamepad"
    files = plg.findall("FILE")
    # first: the files of the version before go (the plugin manager never overwrites a file)
    assert "Name" not in files[0].attrib and files[0].get("Run") == "/bin/bash"
    assert files[0].findtext("INLINE").strip() + "\n" == (SRC / "install/pre-install.sh").read_text()
    assert files[-1].get("Method") == "remove"
    assert files[-1].findtext("INLINE").strip() + "\n" == (SRC / "install/remove.sh").read_text()
    assert files[-2].findtext("INLINE").strip() + "\n" == (SRC / "install/post-install.sh").read_text()
    named = {f.get("Name"): f for f in files if f.get("Name")}
    sources = {
        "MCPanel.page": "0644",
        "mcpanel.css": "0644",
        "mcpanel.js": "0644",
        "include/common.php": "0644",
        "include/action.php": "0644",
        "bin/mcpanel": "0755",
        "event/docker_started": "0755",
    }
    assert set(named) == {f"/usr/local/emhttp/plugins/mcpanel/{rel}" for rel in sources}
    for rel, mode in sources.items():
        node = named[f"/usr/local/emhttp/plugins/mcpanel/{rel}"]
        # a Mode on every file: the plugin manager writes files with the umask of whoever runs it (000 seen)
        assert node.get("Mode") == mode, rel
        # what the plugin manager writes: the INLINE text trimmed, plus one newline
        assert node.findtext("INLINE").strip() + "\n" == (SRC / rel).read_text(), rel
    # never a scripts/ folder: Unraid's StartCommand.php runs anything there with the arguments of a POST
    assert not any("/scripts/" in name for name in named)
    assert len(files) == len(sources) + 3


def test_the_repository_address_is_in_one_variable_of_the_build_script_only():
    base = re.search(r'^BASE_URL="([^"]+)"$', (PLUGIN / "build.sh").read_text(), re.M).group(1)
    hits = subprocess.run(
        ["git", "grep", "-l", "--untracked", "-F", base.split("://", 1)[1]], cwd=REPO, capture_output=True, text=True
    ).stdout.split()
    assert sorted(hits) == ["deploy/unraid/plugin/build.sh", "deploy/unraid/plugin/mcpanel.plg"]
    assert (PLUGIN / "build.sh").read_text().count(base) == 1


def test_changes_are_newest_first():
    versions = re.findall(r"^## (\S+)$", (PLUGIN / "CHANGES.md").read_text(), re.M)
    assert versions == sorted(versions, reverse=True) and len(set(versions)) == len(versions)


def test_the_runner_and_the_page_accept_the_same_tasks():
    runner = (SRC / "bin/mcpanel").read_text()
    assert re.search(r'^TASKS="([^"]+)"$', runner, re.M).group(1).split() == TASKS
    php = (SRC / "include/common.php").read_text()
    assert re.findall(r"^    '([a-z-]+)' => \[", php, re.M) == TASKS
    assert "'update-run' => ['', 'with-migrations']" in php
    assert "'rollback-run' => ['', 'archive-other-games']" in php


# --- what comes in ------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "args",
    [
        ("nonsense",),
        ("status", "with-migrations"),
        ("update-run", "--with-migrations"),
        ("update-run", "with-migrations", "extra"),
        ("rollback-run", "with-migrations"),
        ("update-run", "with-migrations; touch /tmp/x"),
        ("status; id",),
        ("",),
        (),
    ],
)
def test_unknown_tasks_and_odd_arguments_are_refused(host, args):
    host.use_checkout()
    for command in ("describe", "start"):
        done = host.run(command, *args)
        assert done.returncode == 64, (command, args, done.stdout)
        assert done.stdout.startswith("REFUSED: not a task of this plugin") or done.stdout.startswith("usage:")
    assert host.calls() == []
    assert not (host.run_dir / "task.env").exists()


def test_unknown_commands_are_refused(host):
    for args in (("worker", "status"), ("save", "a"), ("facts", "x"), ("rm", "-rf"), ("boot", "now")):
        assert host.run(*args).returncode == 64, args


# --- every task: one fixed command line ----------------------------------------------------------------------
def test_each_task_describes_its_fixed_commands(host):
    host.use_checkout()
    c, d, u = host.checkout, host.deploy, host.unraid
    git = f"git -C {c} -c core.fsmonitor= -c core.hooksPath=/dev/null"
    expected = {
        "status": [
            f"cd {d} && bash scripts/upgrade.sh status",
            f"bash {u}/array-start.sh status --project mcpanel",
        ],
        "check-updates": [
            f"timeout 120 {git} fetch origin main",
            f"{git} log --format=%h\\ %s HEAD..refs/remotes/origin/main",
        ],
        "update-prepare": [f"timeout 300 {git} pull --ff-only", f"cd {d} && bash scripts/upgrade.sh dry-run"],
        "rollback-prepare": [f"cd {d} && bash scripts/upgrade.sh rollback --dry-run"],
        "boot-status": [f"bash {u}/array-start.sh status --project mcpanel"],
        "boot-dry-run": [f"bash {u}/array-start.sh dry-run --project mcpanel"],
        "boot-run": [f"bash {u}/array-start.sh run --project mcpanel"],
        "firewall-status": [f"bash {u}/instance-firewall.sh status"],
        "firewall-dry-run": [f"bash {u}/instance-firewall.sh dry-run"],
        "firewall-install": [f"bash {u}/instance-firewall.sh install"],
        "firewall-remove": [f"bash {u}/instance-firewall.sh remove"],
    }
    for task, lines in expected.items():
        assert described(host, task) == lines, task
    # the runs need a dry run first
    assert described(host, "update-run")[0].startswith("REFUSED: first the dry run ('Update')")
    assert described(host, "rollback-run")[0].startswith("REFUSED: first the dry run ('Roll back')")
    assert host.calls() == []  # describing runs nothing


@pytest.mark.parametrize(
    ("task", "call"),
    [
        ("status", "{d}|upgrade.sh status"),
        ("boot-status", "{h}|array-start.sh status --project mcpanel"),
        ("boot-dry-run", "{h}|array-start.sh dry-run --project mcpanel"),
        ("boot-run", "{h}|array-start.sh run --project mcpanel"),
        ("firewall-status", "{h}|instance-firewall.sh status"),
        ("firewall-dry-run", "{h}|instance-firewall.sh dry-run"),
        ("firewall-install", "{h}|instance-firewall.sh install"),
        ("firewall-remove", "{h}|instance-firewall.sh remove"),
    ],
)
def test_a_task_runs_what_it_describes(host, task, call):
    host.use_checkout()
    lines = described(host, task)
    result, log = host.start_and_wait(task)
    assert result["RESULT"] == "ok", log
    calls = host.calls()  # "<working folder>|<script> <arguments>"
    assert calls[0].split("|", 1)[1] == call.split("|", 1)[1]
    if call.startswith("{d}"):
        assert calls[0].split("|", 1)[0] == str(host.deploy)
    for line in lines:
        assert f"$ {line}" in log, (line, log)
    version = re.search(r"^## (\S+)$", (PLUGIN / "CHANGES.md").read_text(), re.M).group(1)
    assert log.splitlines()[0].startswith(f"MCPanel plugin {version}: {task}, started ")
    assert "Everything below runs as root with umask 022." in log
    assert re.search(r"^Finished: ok at ", log, re.M)


def test_without_a_checkout_the_tasks_say_so(host):
    for task in ("status", "update-prepare", "firewall-install"):
        assert described(host, task) == [
            "REFUSED: no MCPanel checkout is known: set its folder under Settings below (no running stack was found to take it from)"
        ]
    (host.fake / "panels").write_text(f"mcpanel|{host.deploy}\nother|{host.deploy}x\n")
    assert host.facts()["CHECKOUT_FROM"] == "detected"


# --- one action at a time ----------------------------------------------------------------------------------
def test_one_action_at_a_time_and_a_killed_action_never_leaves_the_page_stuck(host):
    host.use_checkout()
    (host.fake / "slow").write_text("30\n")
    done = host.run("start", "status")
    assert done.returncode == 0, done.stdout
    end = time.monotonic() + 10
    while "status" not in "".join(host.calls()) and time.monotonic() < end:
        time.sleep(0.05)
    busy = host.run("start", "firewall-status")
    assert busy.returncode == 75 and busy.stdout.startswith("BUSY: another action is running: status (pid ")
    facts = host.facts()
    assert facts["BUSY"] == "1" and facts["TASK_TASK"] == "status" and facts["TASK_RESULT"] == "running"
    # the worker leads a session of its own: kill it and what it started, the hard way
    pid = int(host.task()["PID"])
    os.killpg(pid, signal.SIGKILL)
    end = time.monotonic() + 10
    while not host.lock_free() and time.monotonic() < end:
        time.sleep(0.05)
    assert host.lock_free(), "the kernel frees the lock when the task and its children are gone"
    facts = host.facts()
    assert facts["BUSY"] == "0" and facts["TASK_RESULT"] == "running"  # the page says: interrupted
    log = Path(host.task()["LOG"])
    assert "upgrade.sh status" in log.read_text()  # the log of the interrupted action stays readable
    (host.fake / "slow").unlink()
    result, _ = host.start_and_wait("firewall-status")
    assert result["RESULT"] == "ok"
    assert log.exists() and len(host.facts()["LOG_FILE"]) == 2


def test_old_logs_are_rotated(host):
    host.use_checkout()
    logs = host.run_dir / "logs"
    logs.mkdir(parents=True)
    for i in range(25):
        (logs / f"20260101-0000{i:02d}-status.log").write_text("old\n")
    host.start_and_wait("firewall-status")
    assert len(list(logs.glob("*.log"))) == 10
    assert not (logs / "20260101-000000-status.log").exists() and (logs / "20260101-000024-status.log").exists()
    # and together below 32 MB (/run is a small tmpfs): big old logs go first
    for i in range(3):
        (logs / f"20260102-0000{i:02d}-status.log").write_bytes(b"x" * (12 * 1024 * 1024))
    host.start_and_wait("firewall-status")
    assert sum(path.stat().st_size for path in logs.glob("*.log")) < 32 * 1024 * 1024


def test_a_failed_task_is_reported_with_its_exit_status(host):
    host.use_checkout()
    (host.fake / "rc_upgrade").write_text("3\n")
    result, log = host.start_and_wait("status")
    assert result["RESULT"] == "failed" and result["EXIT"] != "0"
    assert re.search(r"^Finished: failed \(exit \d+\)", log, re.M)


# --- updates -----------------------------------------------------------------------------------------------
def test_check_updates_fetches_and_never_pulls(host):
    host.use_checkout()
    before = host.head()
    host.upstream_commit("Fix the thing", plugin_version="2026.12.01")
    result, log = host.start_and_wait("check-updates")
    assert result["RESULT"] == "ok", log
    assert host.head() == before
    assert "1 commit(s) behind, 0 ahead" in log and "Fix the thing" in log
    installed = re.search(r"^## (\S+)$", (PLUGIN / "CHANGES.md").read_text(), re.M).group(1)
    assert f"plugin version 2026.12.01 (installed: {installed}): Unraid's Plugins page updates the plugin itself" in log
    facts = host.facts()
    assert facts["BEHIND"] == "1" and facts["AHEAD"] == "0" and facts["UPSTREAM"] == "origin/main"
    assert [c.split(" ", 1)[1] for c in facts["UPDATE_COMMIT"]] == ["Fix the thing"]
    assert facts["PLUGIN_UPSTREAM"] == "2026.12.01" and facts["LOCAL_CHANGES"] == "0"


def test_update_refuses_a_checkout_with_local_changes(host):
    host.use_checkout()
    host.upstream_commit("new")
    script = host.deploy / "docker-compose.yml"
    script.write_text("services: {changed: {}}\n")
    assert described(host, "update-prepare")[0].startswith("REFUSED: the checkout has local changes in 1 file(s)")
    result, log = host.start_and_wait("update-prepare")
    assert result["RESULT"] == "failed" and "REFUSED: the checkout has local changes" in log
    assert host.calls() == []


def test_update_pulls_shows_the_dry_run_and_runs_only_what_it_proposed(host):
    host.use_checkout()
    host.upstream_commit("Add a migration")
    (host.fake / "dry_update").write_text("Nothing was changed. To do it: ./scripts/upgrade.sh run --with-migrations\n")
    result, log = host.start_and_wait("update-prepare")
    assert result["RESULT"] == "ok", log
    assert host.head() == sh("git", "rev-parse", "origin/main", cwd=host.checkout, env=host.env).strip()
    assert host.calls() == [f"{host.deploy}|upgrade.sh dry-run"]
    assert "This version changes the database schema" in log
    assert host.facts()["PLAN_UPDATE"] == "run-with-migrations"
    # without the box: refused, with it: exactly upgrade.sh run --with-migrations
    assert described(host, "update-run")[0].startswith("REFUSED: this version changes the database schema")
    assert described(host, "update-run", "with-migrations") == [
        f"cd {host.deploy} && bash scripts/upgrade.sh run --with-migrations"
    ]
    result, log = host.start_and_wait("update-run")
    assert result["RESULT"] == "failed"
    result, log = host.start_and_wait("update-run", "with-migrations")
    assert result["RESULT"] == "ok", log
    assert host.calls()[-1] == f"{host.deploy}|upgrade.sh run --with-migrations"
    assert log.rstrip().splitlines()[-3].startswith("Panel tabs that were open")  # the script's closing notes, last
    # one run per dry run
    assert described(host, "update-run", "with-migrations")[0].startswith("REFUSED: first the dry run")
    assert "PLAN_UPDATE" not in host.facts()


def test_the_lines_the_plugin_reads_are_the_ones_upgrade_sh_prints():
    """The plan parsing above feeds stand-in lines; these are the real script's (test_upgrade_script.py runs it)."""
    script = (REPO / "deploy/scripts/upgrade.sh").read_text()
    assert 'say "Nothing was changed. To do it: ${LOOSE_REPAIR:+$LOOSE_REPAIR && }$(next_command)"' in script
    assert 'local cmd="./scripts/upgrade.sh run"' in script and 'cmd="$cmd --with-migrations"' in script
    assert (
        'say "Nothing was changed. To do it: ${LOOSE_REPAIR:+$LOOSE_REPAIR && }./scripts/upgrade.sh rollback'
        '$([ "$GAMES_ARCHIVE" = 1 ] && printf \' --archive-other-games\')"'
    ) in script
    real = (REPO / "backend/tests/test_upgrade_script.py").read_text()
    # a version with a new migration file (the friends features: instances 0008) ends its dry run so
    assert 'endswith("Nothing was changed. To do it: ./scripts/upgrade.sh run --with-migrations")' in real
    assert 'endswith("To do it: ./scripts/upgrade.sh rollback --archive-other-games")' in real
    assert (REPO / "backend/apps/instances/migrations/0008_unraid_and_friends.py").exists()


def test_the_plain_run_is_offered_without_migrations_only(host):
    host.use_checkout()
    host.start_and_wait("update-prepare")
    assert host.facts()["PLAN_UPDATE"] == "run"
    assert described(host, "update-run", "with-migrations")[0].startswith(
        "REFUSED: the dry run did not ask for database migrations"
    )
    result, _ = host.start_and_wait("update-run")
    assert result["RESULT"] == "ok" and host.calls()[-1] == f"{host.deploy}|upgrade.sh run"


@pytest.mark.parametrize(
    "last_line",
    [
        "Nothing was changed. To do it: chmod -R go-w /x && ./scripts/upgrade.sh run",
        "Nothing was changed. To do it: ./scripts/upgrade.sh run --from abc1234",
        "Nothing was changed. 'run' would refuse now:",
        "",
    ],
)
def test_a_dry_run_that_proposes_anything_else_offers_nothing(host, last_line):
    host.use_checkout()
    (host.fake / "dry_update").write_text(last_line + "\n")
    _result, log = host.start_and_wait("update-prepare")
    assert "The page offers nothing to run after this dry run" in log
    assert host.facts()["PLAN_UPDATE"] == "none"
    assert described(host, "update-run")[0] == "REFUSED: the dry run proposed nothing the page runs (see its last lines)"


def test_a_dry_run_that_fails_fails_the_update_and_offers_nothing(host):
    host.use_checkout()
    (host.fake / "rc_upgrade").write_text("2\n")
    (host.fake / "dry_update").write_text("upgrade.sh: cannot tell which git revision the running containers were built from\n")
    result, log = host.start_and_wait("update-prepare")
    assert result["RESULT"] == "failed" and "The page offers nothing to run after this dry run" in log
    assert host.facts()["PLAN_UPDATE"] == "none"


def test_facts_name_the_compose_binary_array_start_remembers(host):
    state = host.root / "boot/config/plugins/mcpanel/array-start.env"
    state.parent.mkdir(parents=True)
    state.write_text("# deploy/unraid/array-start.sh: ...\nCOMPOSE_BIN=/mnt/cache/system/docker-compose\nPROJECT=mcpanel\n")
    assert host.facts()["REMEMBERED_COMPOSE_BIN"] == "/mnt/cache/system/docker-compose"


def test_a_plan_is_void_when_the_checkout_or_the_upgrade_state_changed(host):
    host.use_checkout()
    host.start_and_wait("update-prepare")
    assert host.facts()["PLAN_UPDATE"] == "run"
    (host.deploy / "upgrade-state.env").write_text("STATUS=done\n")
    assert "PLAN_UPDATE" not in host.facts()
    assert described(host, "update-run")[0].startswith("REFUSED: the checkout or the upgrade state changed since the dry run")


def test_after_a_rollback_update_checks_the_recorded_branch_out_first(host):
    host.use_checkout()
    host.upstream_commit("new")
    sh("git", "checkout", "-q", "--detach", "HEAD", cwd=host.checkout, env=host.env)
    assert described(host, "update-prepare")[0].startswith("REFUSED: the checkout is not on a branch")
    (host.deploy / "upgrade-state.env").write_text("STATUS=rolled-back\nBRANCH=main\n")
    git = f"git -C {host.checkout} -c core.fsmonitor= -c core.hooksPath=/dev/null"
    assert described(host, "update-prepare")[:2] == [f"{git} checkout main", f"timeout 300 {git} pull --ff-only"]
    result, log = host.start_and_wait("update-prepare")
    assert result["RESULT"] == "ok", log
    assert sh("git", "symbolic-ref", "--short", "HEAD", cwd=host.checkout, env=host.env).strip() == "main"


def test_rollback_runs_only_what_its_dry_run_proposed(host):
    host.use_checkout()
    (host.fake / "dry_rollback").write_text(
        "Nothing was changed. To do it: ./scripts/upgrade.sh rollback --archive-other-games\n"
    )
    result, log = host.start_and_wait("rollback-prepare")
    assert result["RESULT"] == "ok" and host.calls() == [f"{host.deploy}|upgrade.sh rollback --dry-run"]
    assert "The dry run above is what 'Roll back now' does, with --archive-other-games" in log
    assert host.facts()["PLAN_ROLLBACK"] == "rollback-archive"
    assert described(host, "rollback-run")[0].startswith(
        "REFUSED: the dry run says servers of other games must be archived first"
    )
    result, _log = host.start_and_wait("rollback-run", "archive-other-games")
    assert result["RESULT"] == "ok" and host.calls()[-1] == f"{host.deploy}|upgrade.sh rollback --archive-other-games"


def test_tasks_that_change_things_refuse_a_checkout_others_can_write(host):
    host.use_checkout()
    (host.checkout / ".git/config").chmod(0o666)
    for task in ("check-updates", "update-prepare", "rollback-prepare", "firewall-install", "firewall-remove"):
        line = described(host, task)[0]
        assert line.startswith(
            "REFUSED: group or others can write, or another user than root owns, what root runs or git reads"
        ), task
        assert line.endswith(f"chown -R root:root {host.checkout} && chmod -R go-w {host.checkout}"), line
    # read-only ones run (the scripts warn themselves); the unattended boot tasks refuse too (F-14)
    assert described(host, "firewall-status") == [f"bash {host.unraid}/instance-firewall.sh status"]
    assert described(host, "boot-run")[0].startswith("REFUSED: group or others can write")
    assert "HEAD" not in host.facts()  # no git in a checkout others can change


# --- Compose and the boot tasks ------------------------------------------------------------------------------
def pinned_runner(host: Host, payload: bytes) -> Path:
    """The runner with the pin of a stand-in payload (the real pin is the release's SHA-256)."""
    (host.fake / "payload").write_bytes(payload)
    text = host.runner.read_text()
    text, count = re.subn(
        r"^COMPOSE_SHA256=[0-9a-f]{64}$", "COMPOSE_SHA256=" + hashlib.sha256(payload).hexdigest(), text, flags=re.M
    )
    assert count == 1
    host.runner.write_text(text)
    return host.runner


def test_the_pin_is_compose_v5_7_0():
    text = (SRC / "bin/mcpanel").read_text()
    assert "\nCOMPOSE_VERSION=v5.7.0\n" in text
    assert "\nCOMPOSE_SHA256=7758048ad38e580f22559603312874fec8bb638d021a9e64182385fc17cc12a2\n" in text
    assert (
        'COMPOSE_URL="https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64"' in text
    )


def test_install_compose_checks_the_pin_keeps_it_on_the_flash_drive_and_links_it(host):
    pinned_runner(host, b"#!/bin/sh\necho compose\n")
    # a server where root's umask is 000 made these folders by hand
    host.cli.mkdir(parents=True)
    host.cli.parent.chmod(0o777)
    host.cli.chmod(0o777)
    result, log = host.start_and_wait("compose-install")
    assert result["RESULT"] == "ok", log
    store = host.root / "boot/config/plugins/mcpanel/docker-compose-v5.7.0"
    ram = host.root / "usr/local/lib/mcpanel/docker-compose"
    assert store.read_bytes() == ram.read_bytes() == b"#!/bin/sh\necho compose\n"
    assert stat.S_IMODE(ram.stat().st_mode) == 0o755
    assert os.readlink(host.cli / "docker-compose") == str(ram)
    assert stat.S_IMODE(host.cli.stat().st_mode) == stat.S_IMODE(host.cli.parent.stat().st_mode) == 0o755
    assert f"$ chmod go-w {host.cli}" in log
    assert (
        "https://github.com/docker/compose/releases/download/v5.7.0/docker-compose-linux-x86_64"
        in (host.fake / "curl.log").read_text()
    )
    assert host.facts()["OWN_COMPOSE"] == "installed"
    assert not (host.run_dir / "docker-compose.download").exists()


def test_install_compose_refuses_a_download_with_another_checksum(host):
    (host.fake / "payload").write_bytes(b"not the release\n")
    result, log = host.start_and_wait("compose-install")
    assert result["RESULT"] == "failed"
    assert "REFUSED: that is not the pinned SHA-256: the download was deleted, nothing was changed" in log
    assert not (host.root / "boot/config/plugins/mcpanel/docker-compose-v5.7.0").exists()
    assert not (host.cli / "docker-compose").exists()


def test_compose_that_answers_already_is_kept(host):
    pinned_runner(host, b"#!/bin/sh\n")
    (host.fake / "compose_builtin").write_text("")
    result, log = host.start_and_wait("compose-install")
    assert result["RESULT"] == "ok" and "the plugin's copy stays a fallback and is not linked" in log
    assert not (host.cli / "docker-compose").exists()


def test_the_boot_tasks_link_the_plugins_compose_when_no_binary_is_set(host):
    pinned_runner(host, b"#!/bin/sh\n")
    host.start_and_wait("compose-install")
    (host.cli / "docker-compose").unlink()  # a reboot: RAM is empty again
    shutil.rmtree(host.root / "usr/local/lib/mcpanel")
    host.use_checkout()
    result, log = host.start_and_wait("boot-run")
    assert result["RESULT"] == "ok", log
    assert os.readlink(host.cli / "docker-compose") == str(host.root / "usr/local/lib/mcpanel/docker-compose")
    assert host.calls()[-1].endswith("|array-start.sh run --project mcpanel")


def test_a_compose_binary_of_the_settings_is_preferred_while_it_exists(host):
    pinned_runner(host, b"#!/bin/sh\n")
    host.start_and_wait("compose-install")
    (host.cli / "docker-compose").unlink()
    own = host.tmp / "cache/bin/docker-compose"
    own.parent.mkdir(parents=True)
    own.write_text("#!/bin/sh\n")
    own.chmod(0o700)
    own.parent.chmod(0o700)
    assert host.save(str(host.checkout), "", str(own)).returncode == 0
    assert described(host, "boot-run") == [f"bash {host.unraid}/array-start.sh run --project mcpanel --compose-bin {own}"]
    host.start_and_wait("boot-run")
    assert host.calls()[-1].endswith(f"|array-start.sh run --project mcpanel --compose-bin {own}")
    assert not (host.cli / "docker-compose").exists()  # array-start.sh links the set one
    # its pool is not mounted: the plugin's copy instead
    own.unlink()
    _result, log = host.start_and_wait("boot-run")
    assert "is missing or not executable (is its pool mounted?)" in log
    assert os.readlink(host.cli / "docker-compose") == str(host.root / "usr/local/lib/mcpanel/docker-compose")
    assert host.calls()[-1].endswith("|array-start.sh run --project mcpanel")


def test_a_damaged_copy_on_the_flash_drive_is_not_used(host):
    pinned_runner(host, b"#!/bin/sh\n")
    host.start_and_wait("compose-install")
    (host.cli / "docker-compose").unlink()
    (host.root / "boot/config/plugins/mcpanel/docker-compose-v5.7.0").write_bytes(b"#!/bin/sh\necho evil\n")
    host.use_checkout()
    assert host.facts()["OWN_COMPOSE"] == "damaged"
    result, log = host.start_and_wait("boot-run")
    assert result["RESULT"] == "failed"
    assert "does not have the pinned SHA-256 (damaged or replaced): not used" in log
    assert not (host.cli / "docker-compose").exists()
    assert host.calls()[-1].endswith("|array-start.sh run --project mcpanel")  # the rest of the boot tasks still run


def test_the_copy_that_is_moved_into_place_is_the_one_that_was_checked(host):
    # F-13: a copy that differs from the checked file (a flash drive that reads back wrong, a swap in between)
    pinned_runner(host, b"#!/bin/sh\n")
    host.start_and_wait("compose-install")
    (host.cli / "docker-compose").unlink()
    shutil.rmtree(host.root / "usr/local/lib/mcpanel")
    cp = host.bin / "cp"
    cp.write_text('#!/bin/bash\n/bin/cp "$@" && case "${*: -1}" in *docker-compose.new) echo evil >>"${*: -1}" ;; esac\n')
    cp.chmod(0o755)
    host.use_checkout()
    result, log = host.start_and_wait("boot-run")
    assert result["RESULT"] == "failed"
    assert "does not have the pinned SHA-256: not used" in log
    assert not (host.root / "usr/local/lib/mcpanel/docker-compose").exists() and not (host.cli / "docker-compose").exists()
    assert "NEEDS YOU" in (host.fake / "syslog").read_text()


def test_compose_install_checks_the_copy_on_the_flash_drive_too(host):
    pinned_runner(host, b"#!/bin/sh\n")
    cp = host.bin / "cp"
    cp.write_text('#!/bin/bash\n/bin/cp "$@" && case "${*: -1}" in *docker-compose-v5.7.0.new) echo x >>"${*: -1}" ;; esac\n')
    cp.chmod(0o755)
    result, log = host.start_and_wait("compose-install")
    assert result["RESULT"] == "failed" and "the copy on the flash drive does not have the pinned SHA-256" in log
    assert not list((host.root / "boot/config/plugins/mcpanel").glob("docker-compose*"))


def test_the_boot_tasks_refuse_a_checkout_others_can_change_and_say_so(host):
    # F-14: the unattended run uses the strict check (modes and owner) and says what to do
    host.use_checkout()
    (host.unraid / "array-start.sh").chmod(0o777)
    assert described(host, "boot-run")[0].startswith("REFUSED: group or others can write, or another user than root owns")
    result, log = host.start_and_wait("boot-run")
    assert result["RESULT"] == "failed" and host.calls() == []
    assert f"chown -R root:root {host.checkout} && chmod -R go-w {host.checkout}" in log
    assert host.facts()["BOOT_NEEDS"].startswith("boot tasks of the checkout NOT run")
    assert "plugin: boot-run: NEEDS YOU: boot tasks of the checkout NOT run" in (host.fake / "syslog").read_text()
    (host.unraid / "array-start.sh").chmod(0o755)
    # a file root runs that belongs to another user (here: one that is not the runner's)
    target = host.unraid / "stopping_docker"
    target.unlink()
    target.symlink_to("/bin/true")
    if os.stat("/bin/true").st_uid != os.geteuid():
        assert "another user than root owns" in described(host, "boot-run")[0]
        assert "another user than root owns" in host.facts()["CHECKOUT_PROBLEM"]


def test_the_boot_tasks_work_for_a_checkout_without_git(host):
    # the quick start's ZIP download: boot tasks yes, updates through the page no
    shutil.rmtree(host.checkout / ".git")
    host.use_checkout()
    result, log = host.start_and_wait("boot-run")
    assert result["RESULT"] == "ok", log
    assert host.calls()[-1].endswith("|array-start.sh run --project mcpanel")
    for task in ("check-updates", "update-prepare", "rollback-prepare"):
        assert "has no .git folder (a ZIP download?)" in described(host, task)[0], task
    facts = host.facts()
    assert facts["GIT"] == "no" and "CHECKOUT_PROBLEM" in facts and facts["CHECKOUT_PROBLEM"] == ""


def test_the_event_script_returns_at_once_and_the_boot_tasks_run_in_the_background(host):
    (host.fake / "panels").write_text(f"mcpanel|{host.deploy}\n")  # found from the stack's labels: no settings needed
    (host.fake / "slow_boot").write_text("2\n")
    began = time.monotonic()
    done = subprocess.run([str(host.plugin / "event/docker_started"), "docker_started"], env=host.env, timeout=30)
    assert done.returncode == 0 and time.monotonic() - began < 1.5, "emhttp waits for the event script"
    end = time.monotonic() + 20
    while not (host.run_dir / "boot.env").exists() and time.monotonic() < end:
        time.sleep(0.1)
    boot = dict(line.split("=", 1) for line in (host.run_dir / "boot.env").read_text().splitlines())
    assert boot["TASK"] == "boot-run" and boot["RESULT"] == "ok"
    assert [c.split("|", 1)[1] for c in host.calls()] == ["array-start.sh run --project mcpanel"]
    assert "plugin: boot tasks finished: ok" in (host.fake / "syslog").read_text()
    facts = host.facts()
    assert facts["BOOT_RESULT"] == "ok" and facts["CHECKOUT_FROM"] == "detected"


def test_the_boot_tasks_wait_for_a_running_action(host):
    host.use_checkout()
    (host.fake / "slow").write_text("2\n")
    assert host.run("start", "status").returncode == 0
    time.sleep(0.3)
    began = time.monotonic()
    done = host.run("boot")
    assert done.returncode == 0
    assert time.monotonic() - began >= 1
    assert [c.split("|", 1)[1] for c in host.calls()] == [
        "upgrade.sh status",
        "array-start.sh status --project mcpanel",
        "array-start.sh run --project mcpanel",
    ]
    assert "the boot tasks wait for the running action: status" in (host.fake / "syslog").read_text()


# --- settings -----------------------------------------------------------------------------------------------
def test_settings_are_checked_before_they_are_kept(host):
    for args, said in (
        (("relative/path", "", ""), "the checkout must be an absolute path"),
        ((str(host.tmp / "nothing"), "", ""), "is not a folder"),
        ((str(host.tmp), "", ""), "is not an MCPanel checkout"),
        ((str(host.checkout) + '"; x="', "", ""), "the checkout must be an absolute path"),
        (("", "Bad Name", ""), "not a Compose project name"),
        (("", "", str(host.tmp / "missing")), "the Compose binary must be an absolute path to an executable file"),
    ):
        done = host.save(*args)
        assert done.returncode == 1 and said in done.stdout, (args, done.stdout)
    assert not host.cfg.exists()
    host.deploy.chmod(0o777)
    done = host.save(str(host.checkout))
    assert done.returncode == 1 and done.stdout.rstrip().endswith(f"chmod -R go-w {host.checkout}")
    host.deploy.chmod(0o755)
    assert host.save(str(host.checkout), "mystack", "").stdout == f"SAVED: {host.cfg}\n"
    assert host.save(str(host.checkout), "mystack", "").stdout == "SAVED: nothing changed\n"
    assert f'CHECKOUT="{host.checkout}"' in host.cfg.read_text()
    facts = host.facts()
    assert facts["CHECKOUT"] == str(host.checkout) and facts["PROJECT"] == "mystack" and facts["PROJECT_FROM"] == "settings"


def test_the_settings_file_is_never_sourced(host):
    host.cfg.parent.mkdir(parents=True)
    pwned = host.tmp / "pwned"
    host.cfg.write_text(f'CHECKOUT="$(touch {pwned})"\nPROJECT=`touch {pwned}`\nCOMPOSE_BIN="/x"; touch {pwned}\n')
    facts = host.facts()
    assert not pwned.exists()
    assert facts["SET_CHECKOUT"] == "" and facts["SET_PROJECT"] == "" and facts["SET_COMPOSE_BIN"] == ""


def test_detect_finds_the_stack_from_compose_labels(host):
    (host.fake / "panels").write_text(f"mcpanel|{host.deploy}\nelse|/not/a/checkout/deploy\nbad name|{host.deploy}\n")
    assert host.run("detect").stdout == f"DETECTED=mcpanel|{host.checkout}\n"
    facts = host.facts()
    assert facts["CHECKOUT"] == str(host.checkout) and facts["PROJECT_FROM"] == "detected"


def test_facts_name_what_else_installs_the_boot_tasks(host):
    scripts = host.root / "boot/config/plugins/user.scripts/scripts"
    (scripts / "mcpanel boot").mkdir(parents=True)
    (scripts / "mcpanel boot/script").write_text("#!/bin/bash\nbash /mnt/user/appdata/mcpanel-src/deploy/unraid/array-start.sh\n")
    (scripts / "mcpanel boot/name").write_text("MCPanel at array start\n")
    (scripts / "other").mkdir()
    (scripts / "other/script").write_text("echo hi\n")
    (host.root / "boot/config/go").write_text("bash /x/deploy/unraid/install-hook.sh install\n")
    facts = host.facts()
    assert facts["USER_SCRIPT"] == ["MCPanel at array start"] and facts["GO_FILE"] == "yes"


def test_facts_show_the_panels_address_and_the_hook(host):
    host.use_checkout()
    (host.fake / "panels").write_text(f"mcpanel|{host.deploy}\n")
    (host.fake / "port").write_text("0.0.0.0:8080\n")
    facts = host.facts()
    assert facts["PANEL_PUBLISHED"] == "0.0.0.0:8080" and facts["HOOK"] == "missing" and facts["BOOT_EVENT"] == "yes"
    (host.plugin / "event/stopping_docker").write_text("#!/bin/bash\n")
    (host.plugin / "event/stopping_docker").chmod(0o755)
    assert host.facts()["HOOK"] == "installed"


def test_the_run_folder_must_be_roots_own_on_every_use(host):
    host.use_checkout()
    host.start_and_wait("firewall-status")
    assert host.facts()["TASK_RESULT"] == "ok"
    # F-12: a run folder others can write is neither written nor read (facts, plans, the lock)
    host.run_dir.chmod(0o777)
    done = host.run("start", "status")
    assert done.returncode == 1 and "is not a folder of root's that only root can write" in done.stdout
    facts = host.facts()
    assert "RUN_DIR_PROBLEM" in facts and "TASK_RESULT" not in facts and "LOG_FILE" not in facts and facts["BUSY"] == "0"
    assert (
        described(host, "update-run")[0].startswith("REFUSED: ")
        and "is not a folder of root's" in described(host, "update-run")[0]
    )
    host.run_dir.chmod(0o755)
    (host.run_dir / "logs").chmod(0o777)
    assert "RUN_DIR_PROBLEM" in host.facts()
    (host.run_dir / "logs").chmod(0o755)
    shutil.rmtree(host.run_dir)
    host.run_dir.symlink_to(host.tmp)
    done = host.run("start", "status")
    assert done.returncode == 1 and "is not a folder of root's that only root can write" in done.stdout


def test_the_run_folder_is_in_run_not_in_tmp():
    text = (SRC / "bin/mcpanel").read_text()
    assert 'RUN_DIR="${R}/run/mcpanel"' in text and "/tmp/mcpanel" not in text
    assert "'run_dir' => '/run/mcpanel'" in (SRC / "include/common.php").read_text()
    assert '"${R}/run/mcpanel"' in (SRC / "install/remove.sh").read_text()


# --- the scripts the plugin manager runs --------------------------------------------------------------------
def run_script(host: Host, name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SRC / "install" / name)], env=host.env, capture_output=True, text=True, timeout=30)


def test_an_update_replaces_the_files_and_keeps_the_installed_shutdown_hook(host):
    hook = host.plugin / "event/stopping_docker"
    hook.write_text("#!/bin/bash\n# the hook install-hook.sh rendered\n")
    hook.chmod(0o755)
    (host.plugin / "old-file.php").write_text("old\n")
    assert run_script(host, "pre-install.sh").returncode == 0
    assert sorted(p.relative_to(host.plugin).as_posix() for p in host.plugin.rglob("*")) == ["event", "event/stopping_docker"]
    assert hook.read_text().endswith("rendered\n") and stat.S_IMODE(hook.stat().st_mode) == 0o755
    assert not list((host.root / "tmp").glob("mcpanel-hook.*"))


def test_after_the_files_nothing_of_the_plugin_is_writable_by_group_or_others(host):
    (host.plugin / "include").mkdir()
    for path in (host.plugin, host.plugin / "bin", host.plugin / "event", host.plugin / "include"):
        path.chmod(0o770)  # what the plugin manager's mkdir(…, 0770) gives under umask 000
    page = host.plugin / "MCPanel.page"
    page.write_text("x\n")
    page.chmod(0o666)
    done = run_script(host, "post-install.sh")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Seen on one Unraid 7.3.4 server, not yet in every part" in done.stdout
    loose = subprocess.run(["find", str(host.plugin), "-perm", "/022"], capture_output=True, text=True).stdout
    assert loose == ""
    assert stat.S_IMODE(host.runner.stat().st_mode) == 0o755 and stat.S_IMODE(page.stat().st_mode) == 0o644
    assert (host.root / "boot/config/plugins/mcpanel").is_dir()


def test_removal_waits_for_a_running_action_and_keeps_the_settings(host):
    host.use_checkout()
    (host.fake / "slow").write_text("3\n")
    assert host.run("start", "status").returncode == 0
    time.sleep(0.3)
    done = run_script(host, "remove.sh")
    assert done.returncode == 1 and "still running" in done.stdout
    assert host.plugin.exists()
    host.wait()
    done = run_script(host, "remove.sh")
    assert done.returncode == 0, done.stdout
    assert not host.plugin.exists() and not host.run_dir.exists()  # the action logs go with it
    assert host.cfg.exists()
    assert "plugin removed" in (host.fake / "syslog").read_text()


def test_the_runner_and_the_scripts_say_what_has_run_on_unraid_and_what_not():
    """The first run on one Unraid 7.3.4 server (2026-11-12): what ran there, and what was not seen yet."""
    runner = " ".join((SRC / "bin/mcpanel").read_text().split())
    assert "RAN ON ONE REAL UNRAID 7.3.4 SERVER ON 2026-11-12" in runner
    assert "status, check-updates, boot-dry-run, boot-run, firewall-status, firewall-dry-run and firewall-install" in runner
    assert "NOT SEEN THERE YET: boot at a real Docker start (the event), update-prepare/update-run" in runner
    page = " ".join((SRC / "MCPanel.page").read_text().split())
    assert "This plugin has run on one Unraid 7.3.4 server (2026-11-12" in page
    assert "this page itself, Update and Roll back, Install Compose and the boot tasks at a real Docker start" in page
    assert "has not been seen on a real Unraid server yet" in (SRC / "include/common.php").read_text()
    assert "Seen on one Unraid 7.3.4 server, not yet in every part" in (SRC / "install/post-install.sh").read_text()
    assert "Ran on one Unraid 7.3.4 server on 2026-11-12" in (PLUGIN / "mcpanel.plg").read_text()
    for path in [*SRC.rglob("*"), PLUGIN / "build.sh"]:
        if path.is_file():
            assert "NOT RUN ON A REAL UNRAID HOST YET" not in path.read_text(), path


@pytest.mark.parametrize(("rc", "result"), [("3", "ok"), ("0", "ok"), ("1", "failed"), ("2", "failed")])
def test_a_firewall_that_is_not_installed_is_a_normal_status(host, rc, result):
    # the first run on Unraid: "missing (not installed)" ended "Finished: failed (exit 1)" on the page
    host.use_checkout()
    (host.fake / "fw_rc").write_text(rc + "\n")
    task, log = host.start_and_wait("firewall-status")
    assert task["RESULT"] == result, log
    assert ("(not installed: a normal state, nothing failed)" in log) == (rc == "3")
    # what it describes stays the one command
    assert described(host, "firewall-status") == [f"bash {host.unraid}/instance-firewall.sh status"]
