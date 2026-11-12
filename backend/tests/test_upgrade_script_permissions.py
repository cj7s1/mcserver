"""deploy/scripts/upgrade.sh: who can change what root runs (security review 2026-11-11, D-2).

On the owner's Unraid server the checkout sat in an SMB-exported share with the scripts at mode 777
and the compose file at 666: whoever could write them (an SMB user, a container that mounts the
share) could run code as root at the next upgrade. The script now refuses `run`, `rollback` and
`restore-db --yes` while group or others can write what git tracks there, the checkout's folder,
.git, .git/config, .git/hooks or deploy/.env, and names the repair; `status` and the dry runs only
warn. It runs git without fsmonitor and hooks, and under umask 022.

The site (a git checkout with the stand-in docker) is the one of tests/test_upgrade_script.py.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

from .test_upgrade_script import SCRIPT, Site, pytestmark  # noqa: F401 (pytestmark: the same skip)

REFUSED = "refusing: group or others can write to"
WARNED = "WARNING: group or others can write to"


@pytest.fixture
def site(tmp_path: Path) -> Site:
    return Site(tmp_path)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def repair_line(text: str) -> str:
    """The command the message gives (the line after "Repair it ..." / "... until it is repaired")."""
    lines = [line.strip() for line in text.splitlines()]
    for n, line in enumerate(lines):
        if line.endswith(("Repair it (as root), then run this command again:", "until it is repaired (as root):")):
            return lines[n + 1]
    raise AssertionError(text)


def assert_nothing_changed(site: Site, images: dict[str, str]) -> None:
    assert not site.state_file.exists() and not site.logs()
    assert not site.events() and site.images() == images
    assert not list(site.deploy.glob("upgrade-backup-*"))


# --- refusal, warning, repair ------------------------------------------------------------------------
def test_commands_that_change_things_refuse_while_others_can_write_the_checkout(site):
    compose = site.deploy / "docker-compose.yml"
    compose.chmod(0o666)  # as found on the owner's server
    images = site.images()

    for args in (("run", "--with-migrations"), ("rollback",), ("run", "--resume")):
        refused = site.upgrade(*args)
        assert refused.returncode == 2, (args, refused.both)
        assert REFUSED in refused.stderr and "666 deploy/docker-compose.yml" in refused.stderr
        assert repair_line(refused.stderr) == f"chmod -R go-w {shlex.quote(str(site.root))}"
        assert "(umask 022 && git pull)" in refused.stderr
        assert_nothing_changed(site, images)
    dump = site.deploy / "upgrade-backup-x.sql"
    dump.write_text("-- PostgreSQL database dump complete\n")
    refused = site.upgrade("restore-db", str(dump), "--yes")
    assert refused.returncode == 2 and REFUSED in refused.stderr and not site.events("RESTORE")
    dump.unlink()

    # the commands that change nothing say so and go on
    for args in (("status",), ("dry-run",), ("rollback", "--dry-run"), ("restore-db", str(dump))):
        shown = site.upgrade(*args)
        assert WARNED in shown.stderr and REFUSED not in shown.stderr, args
        assert repair_line(shown.stderr) == f"chmod -R go-w {shlex.quote(str(site.root))}", args
    assert site.upgrade("status").returncode == 0
    plan = site.upgrade("dry-run")
    # its last line names the repair first, then the run that works after it (FX-3)
    repair = f"chmod -R go-w {shlex.quote(str(site.root))}"
    assert plan.returncode == 0
    assert plan.stdout.rstrip().splitlines()[-1] == (
        f"Nothing was changed. To do it: {repair} && ./scripts/upgrade.sh run --with-migrations"
    )
    assert_nothing_changed(site, images)

    # the repair it names is enough
    subprocess.run(repair_line(site.upgrade("status").stderr), shell=True, check=True)
    assert mode(compose) == 0o644
    clean = site.upgrade("status")
    assert "group or others" not in clean.both
    done = site.upgrade("run", "--with-migrations")
    assert done.returncode == 0, done.both
    site.assert_new_version_runs()


@pytest.mark.parametrize(
    ("path", "loose"),
    [
        ("deploy/scripts/upgrade.sh", 0o777),
        ("deploy/scripts", 0o777),
        ("backend/apps/jobs/migrations/0002_job_heartbeat_at.py", 0o664),  # rewritten by a pull, built into the image
        ("backend", 0o775),
        (".", 0o777),
        (".git", 0o777),
        (".git/config", 0o666),
        (".git/hooks", 0o777),
        ("deploy/.env", 0o666),
        ("deploy/.env", 0o660),
    ],
    ids=lambda value: f"{value:o}" if isinstance(value, int) else value,
)
def test_each_path_root_depends_on_is_checked(site, path, loose):
    target = site.root / path
    before = mode(target)
    target.chmod(loose)
    refused = site.upgrade("run", "--with-migrations")
    assert refused.returncode == 2 and REFUSED in refused.stderr, refused.both
    shown = os.path.normpath(path)
    assert f"{loose:o} {shown}" in refused.stderr
    target.chmod(before)
    assert site.upgrade("run", "--with-migrations").returncode == 0


def test_a_symbolic_link_for_env_is_checked_where_it_points(site, tmp_path):
    elsewhere = tmp_path / "secrets" / "mcpanel.env"
    elsewhere.parent.mkdir()
    elsewhere.write_text((site.deploy / ".env").read_text())
    elsewhere.chmod(0o666)
    (site.deploy / ".env").unlink()
    (site.deploy / ".env").symlink_to(elsewhere)
    refused = site.upgrade("run", "--with-migrations")
    assert refused.returncode == 2 and "666 deploy/.env" in refused.stderr
    repair = repair_line(refused.stderr)
    assert repair == f"chmod -R go-w {site.root} && chmod go-w {site.deploy / '.env'}"
    subprocess.run(repair, shell=True, check=True)
    assert mode(elsewhere) == 0o644
    assert site.upgrade("run", "--with-migrations").returncode == 0


def test_files_git_does_not_track_are_not_checked(site):
    """Caches and virtual environments of a development checkout are no input of the build."""
    cache = site.root / "backend" / ".venv"
    cache.mkdir()
    (cache / ".lock").write_text("")
    (cache / ".lock").chmod(0o666)
    (site.deploy / "notes.txt").write_text("mine\n")
    (site.deploy / "notes.txt").chmod(0o666)
    done = site.upgrade("run", "--with-migrations")
    assert done.returncode == 0, done.both
    assert "group or others" not in done.both


def test_more_than_eight_are_summed_up(site):
    files = sorted((site.root / "backend" / "apps").rglob("*.py"))
    assert len(files) > 8
    for file in files:
        file.chmod(0o666)
    refused = site.upgrade("run", "--with-migrations")
    assert refused.returncode == 2
    assert f"... and {len(files) - 8} more" in refused.stderr


# --- git as root: no fsmonitor, no hooks; umask 022 ------------------------------------------------------
def test_git_runs_without_fsmonitor_and_hooks(site, tmp_path):
    marks = tmp_path / "marks"
    marks.mkdir()
    site.git("config", "core.fsmonitor", f"touch {marks}/fsmonitor; false")
    hooks = site.root / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("post-checkout", "post-merge", "reference-transaction"):
        (hooks / name).write_text(f"#!/bin/sh\ntouch {marks}/{name}\n")
        (hooks / name).chmod(0o755)
    other = tmp_path / "other-hooks"
    other.mkdir()
    (other / "post-checkout").write_text(f"#!/bin/sh\ntouch {marks}/hooks-path\n")
    (other / "post-checkout").chmod(0o755)

    assert site.upgrade("status").returncode == 0
    assert site.upgrade("run", "--with-migrations").returncode == 0
    site.git("config", "core.hooksPath", str(other))
    back = site.upgrade("rollback")  # git checkout --detach <old revision>
    assert back.returncode == 0, back.both
    site.assert_old_version_runs()
    assert sorted(path.name for path in marks.iterdir()) == []


def test_files_the_rollback_checks_out_are_not_writable_by_others_under_a_loose_umask(site):
    assert site.upgrade("run", "--with-migrations").returncode == 0
    # the shell of the owner's Unraid server left 777/666 files after `git clone`: its umask was 000
    back = subprocess.run(
        ["bash", "-c", 'umask 000 && exec ./scripts/upgrade.sh "$@"', "upgrade.sh", "rollback"],
        cwd=site.deploy,
        env=site.env,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    assert back.returncode == 0, back.stdout + back.stderr
    site.assert_old_version_runs()
    # the checkout rewrote the compose file (the old layout has seven services)
    assert "worker-periodic" in (site.deploy / "docker-compose.yml").read_text()
    assert mode(site.deploy / "docker-compose.yml") == 0o644


def test_the_script_says_so_in_its_comment():
    text = SCRIPT.read_text()
    head = text.split("set -uo pipefail", 1)[0]
    assert "chmod -R go-w <checkout>" in head and "(umask 022 && git pull)" in head
    code = text.split("set -uo pipefail", 1)[1]
    assert code.lstrip().splitlines()[2] == "umask 022"  # before anything runs git
    assert "GIT_SAFE=(-c core.fsmonitor= -c core.hooksPath=/dev/null)" in code
    # every git the script starts goes through those options (each names the folder with -C)
    code = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))
    assert re.findall(r"\bgit -C\b", code) == []
    assert code.count('git "${GIT_SAFE[@]}" -C') == 3  # g(), the file list of the check, ls-remote
