"""deploy/unraid/backup-db.sh: the daily database dump (audit 2026-11-09, backend-backups#5 / deploy#5).

It runs against a stand-in for ``docker`` (written per test) that answers ``ps``, ``inspect`` and
``exec`` the way the real one does for the Compose project; nothing reaches a real Docker.
"""

from __future__ import annotations

import gzip
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "unraid" / "backup-db.sh"

SHIM = r"""#!/bin/bash
# fake docker for backup-db.sh
F=${FAKE:?}
printf '%s\n' "$*" >>"$F/calls.log"
case "$1" in
    ps)
        project=""
        for arg in "$@"; do case "$arg" in label=com.docker.compose.project=*) project=${arg##*=} ;; esac; done
        [ -f "$F/pg_down" ] && exit 0
        [ "$project" = "$(cat "$F/project")" ] && echo pgcontainer1
        exit 0 ;;
    inspect)
        cat "$F/workdir" 2>/dev/null; echo; exit 0 ;;
    exec)
        [ -f "$F/fail_dump" ] && { echo "pg_dump: error: connection to server failed" >&2; exit 1; }
        printf -- '--\n-- PostgreSQL database dump\n--\nCREATE TABLE x ();\n'
        [ -f "$F/cut_dump" ] || printf -- '--\n-- PostgreSQL database dump complete\n--\n'
        exit 0 ;;
esac
echo "fake docker: unhandled $*" >&2
exit 97
"""


@pytest.fixture
def site(tmp_path):
    fake = tmp_path / "fake"
    fake.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(SHIM)
    (bin_dir / "docker").chmod(0o755)
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / ".env").write_text("MCPANEL_SECRET_KEY=s3cret\nAGENT_RESTIC_PASSWORD=pw\n")
    (fake / "project").write_text("mcpanel")
    (fake / "workdir").write_text(str(deploy))
    dest = tmp_path / "Backups" / "mcpanel-db"

    class Site:
        def __init__(self) -> None:
            self.fake, self.dest, self.deploy = fake, dest, deploy
            self.env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE": str(fake), "HOME": str(tmp_path)}

        def run(self, *args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["bash", str(SCRIPT), *args], env=self.env, capture_output=True, text=True, timeout=60, check=False
            )

        def calls(self) -> str:
            path = fake / "calls.log"
            return path.read_text() if path.exists() else ""

        def dumps(self) -> list[str]:
            return sorted(p.name for p in dest.glob("mcpanel-db-*.sql.gz")) if dest.exists() else []

    return Site()


def last_line(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout.strip().splitlines() or [""])[-1]


def test_the_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0
    assert os.access(SCRIPT, os.X_OK)


def test_a_dump_and_the_env_copy_are_written_private(site):
    result = site.run(str(site.dest))
    assert result.returncode == 0, result.stdout + result.stderr
    assert last_line(result).startswith("mcpanel-db backup: OK ")
    [dump] = site.dumps()
    path = site.dest / dump
    assert b"PostgreSQL database dump complete" in gzip.decompress(path.read_bytes())
    env_copy = path.with_name(dump.removesuffix(".sql.gz") + ".env")
    assert env_copy.read_text() == (site.deploy / ".env").read_text()
    assert stat.S_IMODE(site.dest.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(env_copy.stat().st_mode) == 0o600
    assert "--exclude-table-data=django_session" in site.calls()
    assert "label=com.docker.compose.project=mcpanel" in site.calls()
    assert not [p for p in site.dest.iterdir() if ".part" in p.name or ".err" in p.name]


def test_the_newest_fourteen_are_kept(site):
    site.dest.mkdir(parents=True)
    for day in range(1, 17):
        (site.dest / f"mcpanel-db-202611{day:02d}-040000.sql.gz").write_bytes(b"old")
        (site.dest / f"mcpanel-db-202611{day:02d}-040000.env").write_text("old")
    (site.dest / "something-else.txt").write_text("keep me")
    result = site.run(str(site.dest))
    assert result.returncode == 0, result.stdout
    dumps = site.dumps()
    assert len(dumps) == 14
    assert "mcpanel-db-20261101-040000.sql.gz" not in dumps and "mcpanel-db-20261103-040000.sql.gz" not in dumps
    assert not (site.dest / "mcpanel-db-20261101-040000.env").exists()
    assert (site.dest / "mcpanel-db-20261104-040000.env").exists()
    assert (site.dest / "something-else.txt").exists()
    assert "3 old one(s) removed" in last_line(result)


def test_another_project_name(site):
    (site.fake / "project").write_text("panel2")
    assert site.run(str(site.dest)).returncode == 1
    result = site.run("--project", "panel2", str(site.dest))
    assert result.returncode == 0, result.stdout
    assert "label=com.docker.compose.project=panel2" in site.calls()


@pytest.mark.parametrize(
    ("toggle", "message"),
    [
        ("pg_down", "no running postgres container of the Compose project 'mcpanel'"),
        ("fail_dump", "pg_dump in container pgcontainer1 did not finish: pg_dump: error: connection to server failed"),
        ("cut_dump", "the dump is incomplete"),
    ],
)
def test_a_failure_says_why_and_leaves_nothing_behind(site, toggle, message):
    (site.fake / toggle).write_text("")
    result = site.run(str(site.dest))
    assert result.returncode == 1
    assert last_line(result).startswith("mcpanel-db backup: FAILED: ") and message in last_line(result)
    assert not site.dumps()
    assert not [p for p in site.dest.iterdir()] if site.dest.exists() else True


def test_the_env_file_can_be_named_when_the_project_label_is_missing(site, tmp_path):
    (site.fake / "workdir").write_text("")
    assert site.run(str(site.dest)).returncode == 1
    other = tmp_path / "elsewhere.env"
    other.write_text("X=1\n")
    result = site.run("--env", str(other), str(site.dest))
    assert result.returncode == 0, result.stdout
    [dump] = site.dumps()
    assert (site.dest / (dump.removesuffix(".sql.gz") + ".env")).read_text() == "X=1\n"


def test_bad_arguments(site):
    for args in (["relative/dir"], ["--keep", "0", str(site.dest)], ["--project", "Bad Name", str(site.dest)], ["--nope"]):
        result = site.run(*args)
        assert result.returncode == 1 and last_line(result).startswith("mcpanel-db backup: FAILED: "), args


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="needs shellcheck")
def test_shellcheck_has_nothing_to_say():
    result = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout


# --- review 2026-11-10 (finding 28): two runs never overwrite each other's files --------------------------------
FROZEN_DATE = '#!/bin/sh\n[ "$1" = "+%Y%m%d-%H%M%S" ] && { echo 20261110-040000; exit 0; }\nexec /usr/bin/date "$@"\n'


def test_two_runs_in_the_same_second_keep_both_dumps(site):
    fake_date = Path(site.env["PATH"].split(":")[0]) / "date"
    fake_date.write_text(FROZEN_DATE)
    fake_date.chmod(0o755)
    for _ in range(3):
        result = site.run(str(site.dest))
        assert result.returncode == 0, result.stdout + result.stderr
    assert site.dumps() == [
        "mcpanel-db-20261110-040000.sql.gz",
        "mcpanel-db-20261110-040000_2.sql.gz",
        "mcpanel-db-20261110-040000_3.sql.gz",
    ]
    for name in site.dumps():
        assert (site.dest / name.replace(".sql.gz", ".env")).exists()
    assert not (site.dest / ".mcpanel-db.lock").exists()


def test_a_run_refuses_while_another_one_holds_the_lock(site):
    site.dest.mkdir(parents=True)
    lock = site.dest / ".mcpanel-db.lock"
    lock.mkdir()
    holder = subprocess.Popen(["sleep", "30"])
    try:
        (lock / "pid").write_text(f"{holder.pid}\n")
        result = site.run(str(site.dest))
        assert result.returncode == 1
        assert f"another run (pid {holder.pid}) is writing to" in last_line(result)
        assert site.dumps() == [] and lock.exists()  # its lock is left alone
    finally:
        holder.kill()
        holder.wait()
    # the run that held it is gone: the lock is taken over
    result = site.run(str(site.dest))
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(site.dumps()) == 1 and not lock.exists()
