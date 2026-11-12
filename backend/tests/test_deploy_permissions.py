"""deploy/unraid/install-hook.sh and backup-db.sh: who can change what root runs (review 2026-11-11, D-2).

Root runs install-hook.sh at every array start (it installs the shutdown hook, which root runs at
shutdown) and backup-db.sh every night, both unattended, from the checkout in the `appdata` share.
On the owner's server those files were mode 777. When group or others can write the script, the
hook file, the .env it copies or their folder:

* run as root inside the MCPanel checkout, both tighten the checkout themselves (`chmod -R go-w`,
  never through a symbolic link), warn on stderr and in the syslog, and go on: a refusal nobody
  reads would leave the server without its shutdown hook or its nightly dump;
* not as root, for a path outside the checkout, or when the chmod fails, they refuse and name the
  repair (`chmod -R go-w <checkout>` in a checkout, a chmod of the paths found elsewhere).

`install-hook.sh status` only warns. The scripts run here from copies whose modes the tests set,
with stand-ins for `id` (root or not), `chmod` (to make it fail) and `logger`; the stand-in docker
of backup-db.sh is the one of tests/test_backup_db_script.py.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from .test_backup_db_script import SHIM

REPO = Path(__file__).resolve().parents[2]
# MCPANEL_UNRAID_SCRIPTS=/path/to/deploy/unraid runs these tests against other copies of the scripts
UNRAID = Path(os.environ.get("MCPANEL_UNRAID_SCRIPTS") or REPO / "deploy" / "unraid")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# Stand-ins, controlled through files in $STUBS: uid (what `id -u` says), chmod_fails, logger.log
STUB_ID = '#!/bin/sh\nif [ "$1" = -u ]; then cat "$STUBS/uid"; else PATH=/usr/bin:/bin exec id "$@"; fi\n'
STUB_CHMOD = '#!/bin/sh\n[ -e "$STUBS/chmod_fails" ] && exit 1\nPATH=/usr/bin:/bin exec chmod "$@"\n'
STUB_LOGGER = '#!/bin/sh\nprintf "%s\\n" "$*" >>"$STUBS/logger.log"\n'


class Stubs:
    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path / "stubs"
        self.dir.mkdir()
        for name, text in (("id", STUB_ID), ("chmod", STUB_CHMOD), ("logger", STUB_LOGGER)):
            (self.dir / name).write_text(text)
            (self.dir / name).chmod(0o755)
        self.root(False)

    def root(self, on: bool) -> None:
        (self.dir / "uid").write_text("0\n" if on else "1000\n")

    def chmod_fails(self, on: bool = True) -> None:
        if on:
            (self.dir / "chmod_fails").touch()
        else:
            (self.dir / "chmod_fails").unlink(missing_ok=True)

    def logged(self) -> str:
        path = self.dir / "logger.log"
        return path.read_text() if path.exists() else ""

    def env(self, **more: str) -> dict[str, str]:
        return {"PATH": f"{self.dir}:/usr/bin:/bin", "STUBS": str(self.dir), **more}


@pytest.fixture
def stubs(tmp_path: Path) -> Stubs:
    return Stubs(tmp_path)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def checkout(tmp_path: Path) -> Path:
    """A checkout as `git clone` with umask 022 leaves it: deploy/unraid with the three files."""
    top = tmp_path / "mcpanel-src"
    unraid = top / "deploy" / "unraid"
    unraid.mkdir(parents=True)
    (top / ".git").mkdir()
    for name in ("install-hook.sh", "stopping_docker", "backup-db.sh"):
        shutil.copy(UNRAID / name, unraid / name)
        (unraid / name).chmod(0o755)
    (top / "deploy" / ".env").write_text("MCPANEL_SECRET_KEY=s3cret\nAGENT_RESTIC_PASSWORD=pw\n")
    (top / "deploy" / ".env").chmod(0o600)
    (top / "deploy" / "docker-compose.yml").write_text("name: mcpanel\n")
    (top / "deploy" / "docker-compose.yml").chmod(0o644)
    return top


def loosen_more(tmp_path: Path, top: Path) -> Path:
    """Another loose file of the checkout, and a link from the checkout to a loose file outside it."""
    (top / "deploy" / "docker-compose.yml").chmod(0o666)
    outside = tmp_path / "outside.txt"
    outside.write_text("not MCPanel's\n")
    outside.chmod(0o666)
    (top / "deploy" / "link-out").symlink_to(outside)
    return outside


TIGHTENED = "were loose and have been tightened"


# --- install-hook.sh ------------------------------------------------------------------------------------
def installer(stubs: Stubs, script: Path, plugins: Path, *args: str, umask: str = "022") -> subprocess.CompletedProcess[str]:
    env = stubs.env(MCPANEL_HOOK_ROOT=str(plugins), MCPANEL_DISK_CFG=str(plugins / "none.cfg"))
    return subprocess.run(
        ["bash", "-c", f'umask {umask} && exec bash "$0" "$@"', str(script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


LOOSE_HOOK_FILES = pytest.mark.parametrize(
    ("name", "loose"),
    [("install-hook.sh", 0o777), ("stopping_docker", 0o777), ("stopping_docker", 0o666), (".", 0o777), (".", 0o775)],
    ids=["script-777", "hook-777", "hook-666", "folder-777", "folder-775"],
)


@LOOSE_HOOK_FILES
def test_not_as_root_the_installer_refuses_while_others_can_write_what_root_runs(tmp_path, stubs, name, loose):
    top = checkout(tmp_path)
    unraid = top / "deploy" / "unraid"
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    target = plugins / "mcpanel" / "event" / "stopping_docker"
    (unraid / name).chmod(loose)
    script = unraid / "install-hook.sh"

    refused = installer(stubs, script, plugins, "install")
    assert refused.returncode == 1 and "install-hook: refusing: group or others can write" in refused.stderr
    assert f"{loose:o} {unraid / name}" in refused.stderr
    assert refused.stderr.rstrip().splitlines()[-1].strip() == f"chmod -R go-w {top}"
    assert not (plugins / "mcpanel").exists()  # nothing installed
    assert mode(unraid / name) == loose  # nothing changed
    assert "shutdown hook NOT installed" in stubs.logged()

    status = installer(stubs, script, plugins, "status")
    assert status.returncode == 1  # not installed, as before
    assert "WARNING: group or others can write" in status.stdout and f"chmod -R go-w {top}" in status.stdout

    # an installed hook is not removed either while the checkout is open to others
    (unraid / name).chmod(0o755)
    assert installer(stubs, script, plugins, "install").returncode == 0 and target.exists()
    (unraid / name).chmod(loose)
    removed = installer(stubs, script, plugins, "remove")
    assert removed.returncode == 1 and "refusing" in removed.stderr and target.exists()

    # the repair it names is enough
    subprocess.run(["chmod", "-R", "go-w", str(top)], check=True)
    done = installer(stubs, script, plugins, "install")
    assert done.returncode == 0 and "WARNING: group or others" not in done.stdout + done.stderr
    assert installer(stubs, script, plugins, "status").returncode == 0
    assert installer(stubs, script, plugins, "remove").returncode == 0 and not target.exists()


@LOOSE_HOOK_FILES
def test_as_root_the_installer_tightens_the_checkout_and_installs(tmp_path, stubs, name, loose):
    """At an unattended array start a refusal would leave the server without its shutdown hook."""
    top = checkout(tmp_path)
    unraid = top / "deploy" / "unraid"
    outside = loosen_more(tmp_path, top)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    target = plugins / "mcpanel" / "event" / "stopping_docker"
    (unraid / name).chmod(loose)
    stubs.root(True)
    script = unraid / "install-hook.sh"

    # status changes nothing
    status = installer(stubs, script, plugins, "status")
    assert "WARNING: group or others can write" in status.stdout and mode(unraid / name) == loose

    done = installer(stubs, script, plugins, "install")
    assert done.returncode == 0, done.stderr
    assert target.exists() and mode(target) == 0o755
    said = f"install-hook: WARNING: permissions of {top} {TIGHTENED}"
    assert said in done.stderr and "check 'git status' there" in done.stderr
    assert f"{loose:o} {unraid / name}" in done.stderr  # what it found
    assert f"shutdown hook: permissions of {top} {TIGHTENED}" in stubs.logged()
    # the whole checkout, nothing outside it (the link is not followed)
    assert mode(unraid / name) == loose & ~0o022
    assert mode(top / "deploy" / "docker-compose.yml") == 0o644
    assert mode(outside) == 0o666
    # the next start has nothing to say
    again = installer(stubs, script, plugins, "install")
    assert again.returncode == 0 and TIGHTENED not in again.stderr

    # remove does the same
    (unraid / name).chmod(loose)
    removed = installer(stubs, script, plugins, "remove")
    assert removed.returncode == 0 and TIGHTENED in removed.stderr and not target.exists()


def test_as_root_a_chmod_that_fails_still_refuses(tmp_path, stubs):
    top = checkout(tmp_path)
    unraid = top / "deploy" / "unraid"
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (unraid / "stopping_docker").chmod(0o666)
    stubs.root(True)
    stubs.chmod_fails()
    refused = installer(stubs, unraid / "install-hook.sh", plugins, "install")
    assert refused.returncode == 1 and "refusing" in refused.stderr and TIGHTENED not in refused.stderr
    assert not (plugins / "mcpanel").exists()


def test_as_root_a_copy_outside_a_checkout_is_not_changed_and_names_the_paths(tmp_path, stubs):
    """docs/UNRAID-SHUTDOWN.md: the copy in /boot/config/mcpanel for a line in /boot/config/go."""
    copy = tmp_path / "boot" / "config" / "mcpanel"
    copy.mkdir(parents=True)
    for name in ("install-hook.sh", "stopping_docker"):
        shutil.copy(UNRAID / name, copy / name)
        (copy / name).chmod(0o600)  # what Unraid's flash mount shows (fmask 0177)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    stubs.root(True)
    assert installer(stubs, copy / "install-hook.sh", plugins, "install").returncode == 0
    (copy / "stopping_docker").chmod(0o666)
    refused = installer(stubs, copy / "install-hook.sh", plugins, "install")
    assert refused.returncode == 1 and TIGHTENED not in refused.stderr
    assert refused.stderr.rstrip().splitlines()[-1].strip() == f"chmod go-w {copy / 'stopping_docker'}"
    assert mode(copy / "stopping_docker") == 0o666


def test_the_folders_install_makes_are_not_writable_by_others_under_a_loose_umask(tmp_path, stubs):
    top = checkout(tmp_path)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    done = installer(stubs, top / "deploy" / "unraid" / "install-hook.sh", plugins, "install", umask="000")
    assert done.returncode == 0, done.stderr
    assert mode(plugins / "mcpanel") == 0o755 and mode(plugins / "mcpanel" / "event") == 0o755
    assert mode(plugins / "mcpanel" / "event" / "stopping_docker") == 0o755


# --- backup-db.sh ---------------------------------------------------------------------------------------
@pytest.fixture
def dump_site(tmp_path, stubs):
    top = checkout(tmp_path)
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "project").write_text("mcpanel")
    (fake / "workdir").write_text(str(top / "deploy"))
    (stubs.dir / "docker").write_text(SHIM)
    (stubs.dir / "docker").chmod(0o755)
    dest = tmp_path / "Backups" / "mcpanel-db"
    env = stubs.env(FAKE=str(fake), HOME=str(tmp_path))

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        script = top / "deploy" / "unraid" / "backup-db.sh"
        return subprocess.run(["bash", str(script), *args, str(dest)], env=env, capture_output=True, text=True, timeout=60)

    return top, dest, run


def last_line(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout.strip().splitlines() or [""])[-1]


LOOSE_DUMP_FILES = pytest.mark.parametrize(
    ("path", "loose"),
    [
        ("deploy/unraid/backup-db.sh", 0o777),
        ("deploy/unraid/backup-db.sh", 0o775),
        ("deploy/unraid", 0o777),
        ("deploy/.env", 0o666),
        ("deploy/.env", 0o620),
    ],
    ids=["script-777", "script-775", "folder-777", "env-666", "env-620"],
)


@LOOSE_DUMP_FILES
def test_not_as_root_the_daily_dump_refuses_while_others_can_write_what_root_runs_or_copies(dump_site, path, loose):
    top, dest, run = dump_site
    assert last_line(run()).startswith("mcpanel-db backup: OK ")
    before = sorted(dest.iterdir())
    (top / path).chmod(loose)
    refused = run()
    assert refused.returncode == 1
    line = last_line(refused)
    assert line.startswith("mcpanel-db backup: FAILED: group or others can write to what root runs or copies here")
    assert line.endswith(f"then run this again: chmod -R go-w {top}")
    assert f"  {loose:o} {top / path}" in refused.stdout
    assert sorted(dest.iterdir()) == before  # nothing written
    assert mode(top / path) == loose
    subprocess.run(["chmod", "-R", "go-w", str(top)], check=True)
    assert last_line(run()).startswith("mcpanel-db backup: OK ")


@LOOSE_DUMP_FILES
def test_as_root_the_daily_dump_tightens_the_checkout_and_makes_the_dump(tmp_path, stubs, dump_site, path, loose):
    """A refusal nobody reads would leave the panel without its nightly copy."""
    top, dest, run = dump_site
    outside = loosen_more(tmp_path, top)
    (top / path).chmod(loose)
    stubs.root(True)
    done = run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert last_line(done).startswith("mcpanel-db backup: OK ")
    said = f"mcpanel-db backup: WARNING: permissions of {top} {TIGHTENED}"
    assert said in done.stderr and "check 'git status' there" in done.stderr
    assert f"{loose:o} {top / path}" in done.stderr
    assert f"mcpanel-db backup: permissions of {top} {TIGHTENED}" in stubs.logged()
    assert mode(top / path) == loose & ~0o022
    assert mode(top / "deploy" / "docker-compose.yml") == 0o644 and mode(outside) == 0o666
    assert len(list(dest.glob("mcpanel-db-*.sql.gz"))) == 1
    assert TIGHTENED not in run().stderr  # the next night has nothing to say


def test_as_root_a_chmod_that_fails_still_fails_the_dump(stubs, dump_site):
    top, dest, run = dump_site
    (top / "deploy" / "unraid" / "backup-db.sh").chmod(0o777)
    stubs.root(True)
    stubs.chmod_fails()
    failed = run()
    assert failed.returncode == 1 and last_line(failed).startswith("mcpanel-db backup: FAILED: group or others")
    assert TIGHTENED not in failed.stderr and not dest.exists()


@pytest.mark.parametrize("as_root", [False, True], ids=["user", "root"])
def test_an_env_file_outside_the_checkout_is_never_changed_and_gets_its_own_repair(tmp_path, stubs, dump_site, as_root):
    _top, dest, run = dump_site
    stubs.root(as_root)
    other = tmp_path / "elsewhere.env"
    other.write_text("MCPANEL_SECRET_KEY=x\n")
    other.chmod(0o666)
    refused = run("--env", str(other))
    assert refused.returncode == 1 and last_line(refused).endswith(f"then run this again: chmod go-w {other}")
    assert mode(other) == 0o666 and TIGHTENED not in refused.stderr
    assert not dest.exists()  # refused before it made the folder
    other.chmod(0o600)
    assert last_line(run("--env", str(other))).startswith("mcpanel-db backup: OK ")
