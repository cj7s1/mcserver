"""deploy/unraid/install-hook.sh and backup-db.sh: who can change what root runs (review 2026-11-11, D-2).

Root runs install-hook.sh at every array start (it installs the shutdown hook, which root runs at
shutdown) and backup-db.sh every night, both from the checkout in the `appdata` share. On the
owner's server those files were mode 777. Both scripts now refuse while group or others can write
the script, the hook file or the .env it copies, or their folder, and name the repair:
`chmod -R go-w <checkout>` in a checkout, a chmod of the paths found elsewhere. `install-hook.sh
status` only warns. The scripts run here from copies whose modes the tests set; the stand-in docker
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
    return top


# --- install-hook.sh ------------------------------------------------------------------------------------
def installer(script: Path, plugins: Path, *args: str, umask: str = "022") -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin", "MCPANEL_HOOK_ROOT": str(plugins), "MCPANEL_DISK_CFG": str(plugins / "none.cfg")}
    return subprocess.run(
        ["bash", "-c", f'umask {umask} && exec bash "$0" "$@"', str(script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    ("name", "loose"),
    [("install-hook.sh", 0o777), ("stopping_docker", 0o777), ("stopping_docker", 0o666), (".", 0o777), (".", 0o775)],
    ids=["script-777", "hook-777", "hook-666", "folder-777", "folder-775"],
)
def test_the_installer_refuses_while_others_can_write_what_root_runs(tmp_path, name, loose):
    top = checkout(tmp_path)
    unraid = top / "deploy" / "unraid"
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    target = plugins / "mcpanel" / "event" / "stopping_docker"
    (unraid / name).chmod(loose)
    script = unraid / "install-hook.sh"

    refused = installer(script, plugins, "install")
    assert refused.returncode == 1 and "install-hook: refusing: group or others can write" in refused.stderr
    assert f"{loose:o} {(unraid / name) if name != '.' else unraid}" in refused.stderr
    assert refused.stderr.rstrip().splitlines()[-1].strip() == f"chmod -R go-w {top}"
    assert not (plugins / "mcpanel").exists()  # nothing installed

    status = installer(script, plugins, "status")
    assert status.returncode == 1  # not installed, as before
    assert "WARNING: group or others can write" in status.stdout and f"chmod -R go-w {top}" in status.stdout

    # an installed hook is not removed either while the checkout is open to others
    (unraid / name).chmod(0o755)
    assert installer(script, plugins, "install").returncode == 0 and target.exists()
    (unraid / name).chmod(loose)
    removed = installer(script, plugins, "remove")
    assert removed.returncode == 1 and "refusing" in removed.stderr and target.exists()

    # the repair it names is enough
    subprocess.run(["chmod", "-R", "go-w", str(top)], check=True)
    done = installer(script, plugins, "install")
    assert done.returncode == 0 and "WARNING: group or others" not in done.stdout
    assert installer(script, plugins, "status").returncode == 0
    assert installer(script, plugins, "remove").returncode == 0 and not target.exists()


def test_a_copy_outside_a_checkout_names_the_paths_to_repair(tmp_path):
    """docs/UNRAID-SHUTDOWN.md: the copy in /boot/config/mcpanel for a line in /boot/config/go."""
    copy = tmp_path / "boot" / "config" / "mcpanel"
    copy.mkdir(parents=True)
    for name in ("install-hook.sh", "stopping_docker"):
        shutil.copy(UNRAID / name, copy / name)
        (copy / name).chmod(0o600)  # what Unraid's flash mount shows (fmask 0177)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    assert installer(copy / "install-hook.sh", plugins, "install").returncode == 0
    (copy / "stopping_docker").chmod(0o666)
    refused = installer(copy / "install-hook.sh", plugins, "install")
    assert refused.returncode == 1
    assert refused.stderr.rstrip().splitlines()[-1].strip() == f"chmod go-w {copy / 'stopping_docker'}"


def test_the_folders_install_makes_are_not_writable_by_others_under_a_loose_umask(tmp_path):
    top = checkout(tmp_path)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    done = installer(top / "deploy" / "unraid" / "install-hook.sh", plugins, "install", umask="000")
    assert done.returncode == 0, done.stderr
    assert mode(plugins / "mcpanel") == 0o755 and mode(plugins / "mcpanel" / "event") == 0o755
    assert mode(plugins / "mcpanel" / "event" / "stopping_docker") == 0o755


# --- backup-db.sh ---------------------------------------------------------------------------------------
@pytest.fixture
def dump_site(tmp_path):
    top = checkout(tmp_path)
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "project").write_text("mcpanel")
    (fake / "workdir").write_text(str(top / "deploy"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(SHIM)
    (bin_dir / "docker").chmod(0o755)
    dest = tmp_path / "Backups" / "mcpanel-db"
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE": str(fake), "HOME": str(tmp_path)}

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        script = top / "deploy" / "unraid" / "backup-db.sh"
        return subprocess.run(["bash", str(script), *args, str(dest)], env=env, capture_output=True, text=True, timeout=60)

    return top, dest, run


def last_line(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout.strip().splitlines() or [""])[-1]


@pytest.mark.parametrize(
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
def test_the_daily_dump_refuses_while_others_can_write_what_root_runs_or_copies(dump_site, path, loose):
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
    subprocess.run(["chmod", "-R", "go-w", str(top)], check=True)
    assert last_line(run()).startswith("mcpanel-db backup: OK ")


def test_an_env_file_outside_the_checkout_gets_its_own_repair(dump_site, tmp_path):
    _top, dest, run = dump_site
    other = tmp_path / "elsewhere.env"
    other.write_text("MCPANEL_SECRET_KEY=x\n")
    other.chmod(0o666)
    refused = run("--env", str(other))
    assert refused.returncode == 1 and last_line(refused).endswith(f"then run this again: chmod go-w {other}")
    assert not dest.exists()  # refused before it made the folder
    other.chmod(0o600)
    assert last_line(run("--env", str(other))).startswith("mcpanel-db backup: OK ")
