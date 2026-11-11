"""deploy/scripts/upgrade.sh, run for real against a stand-in for ``docker``.

The script is the one of this source tree; ``git`` is the real one, working on a small site that
is built here: an OLD revision with the seven-container layout and a NEW one with three
containers, this script and three database migrations. ``docker`` is tests/upgrade_shim/docker:
it keeps containers, image names and the applied migrations in text files, never reaches a real
Docker, and can make single steps fail. The same sequences with real containers are in
.github/scripts/upgrade-rehearsal.sh (CI job "upgrade").

MCPANEL_UPGRADE_SCRIPT=/path/to/another/upgrade.sh runs these tests against that copy.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = Path(os.environ.get("MCPANEL_UPGRADE_SCRIPT") or REPO / "deploy" / "scripts" / "upgrade.sh")
SHIM = Path(__file__).resolve().parent / "upgrade_shim" / "docker"

pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("git") and shutil.which("timeout") and SCRIPT.exists()),
    reason="needs bash, git, coreutils and the deploy/ folder of the repository",
)

OLD_SERVICES = ("agent", "beat", "panel", "postgres", "redis", "worker", "worker-periodic")
NEW_SERVICES = ("agent", "panel", "postgres")
OLD_PANEL, OLD_AGENT, OLD_PG, OLD_REDIS = "sha256:panel-old", "sha256:agent-old", "sha256:pg-old", "sha256:redis-old"
# ten apps, as a real installation has them (the state file then has ten MIGRATION lines)
OLD_MIGRATIONS = {
    "accounts": "0004_reset_token_password_binding",
    "audit": "0002_initial",
    "backups": "0002_initial",
    "core": "0001_initial",
    "instances": "0002_initial",
    "jobs": "0001_initial",
    "metrics": "0001_initial",
    "modpacks": "0001_initial",
    "nodes": "0001_initial",
    "notifications": "0001_initial",
}
UPGRADE_MIGRATIONS = {"instances": "0003_resume_after_boot", "jobs": "0002_job_heartbeat_at", "nodes": "0002_node_tls"}
NEW_MIGRATIONS = {**OLD_MIGRATIONS, **UPGRADE_MIGRATIONS}
DID_NOT_FINISH = "the last run did not finish"
# the name Compose gives the replacement of a container until the old one is gone
TEMP_PANEL = "0123456789ab_mcpanel-panel-1"


def compose_file(services: tuple[str, ...]) -> str:
    return "name: mcpanel\nservices:\n" + "".join(f"  {name}:\n    image: x\n" for name in services) + "volumes: {}\n"


class Site:
    """A deployment: the git checkout with deploy/.env, and the world the stand-in docker keeps."""

    # the last migration per app of the old revision, and those the new revision adds
    old_migrations: dict[str, str] = OLD_MIGRATIONS
    upgrade_migrations: dict[str, str] = UPGRADE_MIGRATIONS

    @property
    def new_migrations(self) -> dict[str, str]:
        return {**self.old_migrations, **self.upgrade_migrations}

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "site"
        self.deploy = self.root / "deploy"
        self.fake = tmp_path / "fake"
        self.bin = tmp_path / "bin"
        self.state_file = self.deploy / "upgrade-state.env"
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "FAKE": str(self.fake),
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "LC_ALL": "C",
        }
        self.bin.mkdir()
        shutil.copy(SHIM, self.bin / "docker")
        (self.bin / "docker").chmod(0o755)
        self._build_checkout()
        self._build_world()

    # -- the checkout ------------------------------------------------------------------
    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *args], env=self.env, capture_output=True, text=True, check=True
        ).stdout.strip()

    def _migration(self, app: str, name: str) -> None:
        path = self.root / "backend" / "apps" / app / "migrations" / f"{name}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# migration\n")

    def _build_checkout(self) -> None:
        self.deploy.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        (self.root / ".gitignore").write_text("deploy/.env\ndeploy/*.env\ndeploy/*.sql\ndeploy/*.sql.part\n*.log\n")
        (self.deploy / "docker-compose.yml").write_text(compose_file(OLD_SERVICES))
        for app, name in self.old_migrations.items():
            self._migration(app, name)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "old layout")
        self.old = self.git("rev-parse", "HEAD")
        # the new version arrives the way `git pull` brings it: a fast-forward that sets ORIG_HEAD
        self.git("checkout", "-q", "-b", "upstream")
        (self.deploy / "docker-compose.yml").write_text(compose_file(NEW_SERVICES))
        (self.deploy / "scripts").mkdir()
        shutil.copy(SCRIPT, self.deploy / "scripts" / "upgrade.sh")
        (self.deploy / "scripts" / "upgrade.sh").chmod(0o755)
        for app, name in self.upgrade_migrations.items():
            self._migration(app, name)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "new layout")
        self.new = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "main")
        self.pull()
        (self.deploy / ".env").write_text("MCPANEL_SECRET_KEY=test\nPOSTGRES_PASSWORD=test\n")

    def pull(self) -> None:
        self.git("merge", "-q", "--ff-only", "upstream")

    def second_pull(self) -> str:
        """Another commit arrives upstream and is pulled: ORIG_HEAD no longer is the deployed revision."""
        self.git("checkout", "-q", "upstream")
        (self.root / "NEWS").write_text("a fix\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "a fix")
        newest = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "main")
        self.pull()
        return newest

    # -- the world of the stand-in docker ------------------------------------------------
    def _build_world(self) -> None:
        (self.fake / "code").mkdir(parents=True)
        self.write(
            "images", [f"mcpanel/panel:local|{OLD_PANEL}", f"mcpanel/agent:local|{OLD_AGENT}", f"postgres:16-alpine|{OLD_PG}"]
        )
        self.write("images", [*self.read("images"), f"redis:7-alpine|{OLD_REDIS}"])
        image = {"agent": ("mcpanel/agent:local", OLD_AGENT), "postgres": ("postgres:16-alpine", OLD_PG)}
        image["redis"] = ("redis:7-alpine", OLD_REDIS)
        rows = []
        for n, service in enumerate(OLD_SERVICES):
            name, image_id = image.get(service, ("mcpanel/panel:local", OLD_PANEL))
            rows.append(f"o{n}|{service}|running|False|{name}|{image_id}|healthy")
        self.write("containers", rows)
        old = [f"{app} {name}" for app, name in sorted(self.old_migrations.items())]
        self.write("migs", old)
        self.write("code/sha256_panel-old", old)
        for name in ("labels", "events.log", "calls.log", "servers"):
            self.write(name, [])
        (self.fake / "redis_volume").touch()

    def read(self, name: str) -> list[str]:
        path = self.fake / name
        return path.read_text().splitlines() if path.exists() else []

    def write(self, name: str, lines: list[str]) -> None:
        (self.fake / name).write_text("".join(f"{line}\n" for line in lines))

    def toggle(self, name: str, on: bool = True) -> None:
        if on:
            (self.fake / name).touch()
        else:
            (self.fake / name).unlink(missing_ok=True)

    def events(self, prefix: str = "") -> list[str]:
        return [line for line in self.read("events.log") if line.startswith(prefix)]

    def forget_events(self) -> None:
        self.write("events.log", [])

    def images(self) -> dict[str, str]:
        return dict(line.split("|", 1) for line in self.read("images"))

    def rollback_tags(self) -> list[str]:
        return sorted(name for name in self.images() if name.startswith("mcpanel-rollback/"))

    def containers(self) -> dict[str, str]:
        """service -> image id"""
        return {row.split("|")[1]: row.split("|")[5] for row in self.read("containers")}

    def container_names(self) -> list[str]:
        rows = [row.split("|") for row in self.read("containers")]
        return sorted(row[7] if len(row) > 7 else f"mcpanel-{row[1]}-1" for row in rows)

    def container_states(self) -> dict[str, str]:
        """container name -> state"""
        rows = [row.split("|") for row in self.read("containers")]
        return {(row[7] if len(row) > 7 else f"mcpanel-{row[1]}-1"): row[2] for row in rows}

    def logs(self) -> list[Path]:
        """the logs of run / rollback / restore-db, oldest first"""
        return sorted(self.deploy.glob("upgrade-log-*.log"), key=lambda path: path.stat().st_mtime_ns)

    @contextlib.contextmanager
    def started(self, *args: str):
        """The script in a session of its own, like a command typed into a terminal. Whatever the
        test does with it, nothing of it stays behind: a stand-in `docker compose` that waits (the
        toggles hang_up and hang_check) may be in a session of its own, where a kill of the
        script's process group does not get to it, so it is let go as well."""
        process = subprocess.Popen(
            ["./scripts/upgrade.sh", *args],
            cwd=self.deploy,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            yield process
        finally:
            self.toggle("release")
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=30)

    def wait_for_event(self, event: str, what: str) -> None:
        deadline = time.monotonic() + 60
        while event not in self.events() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert event in self.events(), what

    def half_recreate_the_panel(self) -> None:
        """A switch whose `docker compose up` was cut off while it replaced the panel container
        (a kill, a host that went down): the agent is made again and not started, the old panel is
        stopped, and its replacement exists under Compose's temporary name, never started."""
        self.toggle("fail_up")
        assert self.upgrade("run", "--with-migrations").returncode == 1
        self.toggle("fail_up", False)
        assert self.status() == "switching"
        names = self.images()
        rows = []
        for row in self.read("containers"):
            fields = row.split("|")
            if fields[1] == "agent":
                fields[2], fields[5], fields[6] = "created", names["mcpanel/agent:local"], "-"
            if fields[1] == "panel":
                fields[2], fields[6] = "exited", "unhealthy"
                new = ["n1", "panel", "created", "False", "mcpanel/panel:local", names["mcpanel/panel:local"], "-", TEMP_PANEL]
                rows.append("|".join(new))
            rows.append("|".join(fields))
        self.write("containers", rows)
        self.forget_events()

    def migrations(self) -> dict[str, str]:
        return dict(line.split(" ", 1) for line in self.read("migs"))

    def stop_services(self, *services: str) -> None:
        rows = []
        for row in self.read("containers"):
            fields = row.split("|")
            if fields[1] in services:
                fields[2] = "exited"
            rows.append("|".join(fields))
        self.write("containers", rows)

    # -- the script ----------------------------------------------------------------------
    def state(self) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        if self.state_file.exists():
            for line in self.state_file.read_text().splitlines():
                if line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    found.setdefault(key, []).append(value)
        return found

    def status(self) -> str:
        return (self.state().get("STATUS") or [""])[0]

    def recorded_images(self) -> dict[str, str]:
        """image name -> image id of the rollback point"""
        return {entry.split("|")[0]: entry.split("|")[1] for entry in self.state().get("IMAGE", [])}

    def upgrade(self, *args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        # restore_signals=False: the script starts with SIGPIPE ignored (Python ignores it), as it
        # does in a systemd unit. A writer whose reader left early then gets an error instead of
        # dying silently, and says so: "printf: write error: Broken pipe".
        result = subprocess.run(
            ["./scripts/upgrade.sh", *args],
            cwd=self.deploy,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            restore_signals=False,
        )
        assert not (self.fake / "unhandled.log").exists(), (self.fake / "unhandled.log").read_text()
        result.both = result.stdout + result.stderr  # type: ignore[attr-defined]
        assert "Broken pipe" not in result.both, result.both
        return result

    def next_second(self) -> None:
        """The names of rollback tags and dumps carry the second they were made in."""
        time.sleep(1.1)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def assert_old_version_runs(self) -> None:
        running = self.containers()
        assert tuple(sorted(running)) == OLD_SERVICES
        assert running["panel"] == OLD_PANEL and running["agent"] == OLD_AGENT and running["postgres"] == OLD_PG
        assert self.migrations() == self.old_migrations
        assert self.head() == self.old

    def assert_old_version_runs_but_tree_is_new(self) -> None:
        running = self.containers()
        assert tuple(sorted(running)) == OLD_SERVICES
        assert running["panel"] == OLD_PANEL and running["agent"] == OLD_AGENT and self.migrations() == self.old_migrations
        assert set(self.container_states().values()) == {"running"}

    def assert_new_version_runs(self, revision: str | None = None) -> None:
        running = self.containers()
        assert tuple(sorted(running)) == NEW_SERVICES
        assert running["panel"] == f"sha256:panel-new-{(revision or self.new)[:12]}"
        assert self.migrations() == self.new_migrations

    def assert_rollback_point_is_the_old_version(self) -> None:
        assert self.state()["FROM_REV"] == [self.old]
        recorded = self.recorded_images()
        assert recorded["mcpanel/panel:local"] == OLD_PANEL and recorded["mcpanel/agent:local"] == OLD_AGENT
        assert recorded["postgres:16-alpine"] == OLD_PG and recorded["redis:7-alpine"] == OLD_REDIS
        tagged = self.images()
        for entry in self.state()["IMAGE"]:
            _name, image_id, tag = entry.split("|")
            assert tagged.get(tag) == image_id, f"the rollback tag {tag} is gone or moved"
        assert [f"{app} {name}" for app, name in sorted(self.old_migrations.items())] == self.state()["MIGRATION"]


@pytest.fixture
def site(tmp_path: Path) -> Site:
    return Site(tmp_path)


# --- the plain way: run, rollback, run again ------------------------------------------------------
def test_run_then_rollback_then_run_again(site):
    plan = site.upgrade("dry-run")
    assert plan.returncode == 0 and not site.state_file.exists() and not site.logs()
    assert "database migrations in this version (run needs --with-migrations)" in plan.stdout and not site.events()
    # the command it ends with is the one that works
    assert plan.stdout.rstrip().endswith("Nothing was changed. To do it: ./scripts/upgrade.sh run --with-migrations")
    # of the old containers Compose itself only removes its orphans: the block names all four
    assert (
        "removed by this script before that: mcpanel-beat-1 mcpanel-redis-1 mcpanel-worker-1 mcpanel-worker-periodic-1"
        in plan.stdout
    )
    assert "Container mcpanel-worker-1" not in plan.stdout and plan.stdout.count("Container mcpanel-panel-1  Recreate") == 1

    done = site.upgrade("run", "--with-migrations")
    assert done.returncode == 0, done.both
    site.assert_new_version_runs()
    assert site.status() == "done" and site.state()["DEPLOYED_REV"] == [site.new]
    site.assert_rollback_point_is_the_old_version()
    assert oct(site.state_file.stat().st_mode & 0o777) == "0o600"
    assert site.events("RM ") == ["RM beat", "RM redis", "RM worker", "RM worker-periodic"]
    # the summary names the images of the containers that run now
    assert "mcpanel/panel:local: panel-old -> panel-new-" in done.stdout
    assert "redis:7-alpine: redis-old -> no running container uses it any more" in done.stdout
    # what it printed is in a log as well: mode 600, named when it starts to change things and by `status`
    (log,) = site.logs()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    text = log.read_text().splitlines()
    assert text[0].startswith("# pid ") and "| ./scripts/upgrade.sh run --with-migrations | started " in text[0]
    assert text[-1].startswith("# ended ") and text[-1].endswith("exit status 0")
    assert f"Log: {log}" in done.stdout and "==> Done" in text and "  to go back: ./scripts/upgrade.sh rollback" in text
    assert [line for line in text if line.startswith("  from:")]  # the plan, printed before the log began, is in it
    assert " Container up (fake)" in text  # ... and what docker compose wrote
    status = site.upgrade("status")
    assert f"Log:          {log}" in status.stdout and "exit status 0" in status.stdout
    assert site.logs() == [log]  # status and dry-run write none
    site.forget_events()

    back = site.upgrade("rollback")
    assert back.returncode == 0, back.both
    site.assert_old_version_runs()
    assert site.status() == "rolled-back"
    assert len(site.logs()) == 2 and "==> Rolled back" in site.logs()[-1].read_text()
    # every app's migrations were undone, not only the first one's: `docker compose run` reads its
    # standard input to the end, which was the rest of the list
    assert sorted(site.events("UNMIGRATE ")) == [
        "UNMIGRATE instances -> 0002_initial",
        "UNMIGRATE jobs -> 0001_initial",
        "UNMIGRATE nodes -> 0001_initial",
    ]
    # ... after the upgraded version was asked whether each can be undone, before anything was stopped
    events = site.events()
    assert max(i for i, e in enumerate(events) if e.startswith("PLAN ")) < min(
        i for i, e in enumerate(events) if e.startswith("STOP ")
    )
    # the old revision does not have the script: the last words say how to get it back
    assert not (site.deploy / "scripts" / "upgrade.sh").exists()
    assert "gone from disk" in back.stdout and "git checkout main" in back.stdout
    # ... and how to upgrade again, with the option that upgrade needs
    assert "git checkout main && cd deploy && ./scripts/upgrade.sh run --with-migrations" in back.stdout
    # ... and which checkout this is, and in which one a plain `docker compose up -d` is safe
    words = " ".join(back.stdout.split())
    assert f"is now at the old version, {site.old[:12]}, as a detached HEAD" in words
    assert "in this checkout 'docker compose up -d' and the other docker compose commands are safe" in words
    assert "do NOT run 'docker compose up -d' in that checkout" in words
    assert f"git checkout --detach {site.old[:12]}" in words

    site.git("checkout", "-q", "main")
    assert "./scripts/upgrade.sh run --with-migrations" in site.upgrade("rollback").stderr  # (rolled back already)
    # the tree is the new version again while the old one runs: status says which checkout fits
    status = " ".join(site.upgrade("status").stdout.split())
    assert "do not run 'docker compose up -d' here" in status and f"git checkout --detach {site.old[:12]}" in status
    same = site.upgrade("dry-run", "--no-build")
    assert "--no-build: the images stay as they are" in same.stdout and "rebuilt" not in same.stdout
    site.next_second()
    again = site.upgrade("run", "--with-migrations")
    assert again.returncode == 0, again.both
    site.assert_new_version_runs()
    site.assert_rollback_point_is_the_old_version()
    assert len(site.rollback_tags()) == 4  # one generation: the tags of the first run were dropped


def test_run_no_build_applies_the_configuration_and_leaves_the_rollback_point_alone(site):
    assert site.upgrade("run", "--with-migrations").returncode == 0
    before = site.state_file.read_text()
    site.forget_events()
    plan = site.upgrade("dry-run", "--no-build")
    assert plan.stdout.rstrip().endswith("To do it: ./scripts/upgrade.sh run --no-build")
    applied = site.upgrade("run", "--no-build")
    assert applied.returncode == 0, applied.both
    assert site.state_file.read_text() == before
    assert [event.split()[0] for event in site.events()] == ["UP-ENV", "UP"]  # no pull, no build, no dump, no tag
    site.assert_new_version_runs()


def test_resume_needs_an_unfinished_run(site):
    result = site.upgrade("run", "--resume")
    assert result.returncode == 1 and "no unfinished run to resume" in result.stderr
    assert not site.state_file.exists() and not site.events()


# --- finding 1, case A: the new version came up but did not become healthy ------------------------
def unhealthy_run(site: Site) -> None:
    site.toggle("unhealthy_panel")
    first = site.upgrade("run", "--with-migrations", "--timeout", "0")
    assert first.returncode == 1 and "not healthy" in first.stderr and "run --resume" in first.stderr
    site.toggle("unhealthy_panel", False)
    assert site.status() == "switching"
    assert site.containers()["panel"].startswith("sha256:panel-new-") and site.migrations() == NEW_MIGRATIONS
    site.assert_rollback_point_is_the_old_version()


def test_a_run_on_top_of_an_unhealthy_run_refuses_and_rollback_still_returns_to_the_old_version(site):
    unhealthy_run(site)
    before = site.state_file.read_text()
    tags = site.rollback_tags()
    site.forget_events()

    second = site.upgrade("run", "--with-migrations")
    assert second.returncode == 2, second.both
    assert DID_NOT_FINISH in second.stderr and "'rollback' first" in second.stderr and "run --resume" in second.stderr
    # nothing was recorded on top of it and nothing was built or switched
    assert site.state_file.read_text() == before and site.rollback_tags() == tags and not site.events()
    assert site.upgrade("run", "--no-build").returncode == 2  # not that way either
    plan = site.upgrade("dry-run")
    assert plan.returncode == 0 and "'run' would refuse now" in plan.stdout and DID_NOT_FINISH in plan.stdout
    status = site.upgrade("status")
    assert "DID NOT FINISH" in status.stdout
    assert site.state_file.read_text() == before and not site.events()

    back = site.upgrade("rollback")
    assert back.returncode == 0, back.both
    site.assert_old_version_runs()


def test_resume_after_an_unhealthy_run_keeps_the_rollback_point(site):
    unhealthy_run(site)
    dump = site.state()["BACKUP_SQL"]
    tags = site.rollback_tags()

    resumed = site.upgrade("run", "--resume")
    assert resumed.returncode == 0, resumed.both
    assert "makes no new one" in resumed.stdout
    site.assert_new_version_runs()
    assert site.status() == "done"
    site.assert_rollback_point_is_the_old_version()
    assert site.rollback_tags() == tags
    # the database has the new schema already: the dump from before the upgrade stays the way back
    assert site.state()["BACKUP_SQL"] == dump and "no new dump" in resumed.stdout and Path(dump[0]).exists()

    back = site.upgrade("rollback")
    assert back.returncode == 0, back.both
    site.assert_old_version_runs()


# --- finding 1, case B: compose up failed after the old workers were removed ------------------------
def test_a_run_after_a_failed_compose_up_refuses_and_resume_keeps_the_real_origin(site):
    site.toggle("fail_up")
    first = site.upgrade("run", "--with-migrations")
    assert first.returncode == 1 and "docker compose up failed" in first.stderr
    site.toggle("fail_up", False)
    assert site.status() == "switching"
    # the old workers are gone, the rest still is the old version: this fits no revision's layout
    assert site.containers() == {"agent": OLD_AGENT, "panel": OLD_PANEL, "postgres": OLD_PG}
    site.assert_rollback_point_is_the_old_version()
    before = site.state_file.read_text()

    second = site.upgrade("run", "--with-migrations")
    assert second.returncode == 2 and DID_NOT_FINISH in second.stderr
    assert "assumed" not in second.both  # the origin is never guessed from the layout now
    assert site.state_file.read_text() == before

    resumed = site.upgrade("run", "--resume")  # --with-migrations was given to the run it continues
    assert resumed.returncode == 0, resumed.both
    site.assert_new_version_runs()
    assert site.status() == "done"
    site.assert_rollback_point_is_the_old_version()

    back = site.upgrade("rollback")
    assert back.returncode == 0, back.both
    site.assert_old_version_runs()


def test_rollback_right_after_a_failed_compose_up(site):
    site.toggle("fail_up")
    assert site.upgrade("run", "--with-migrations").returncode == 1
    site.toggle("fail_up", False)
    back = site.upgrade("rollback")
    assert back.returncode == 0, back.both
    site.assert_old_version_runs()
    assert not site.events("UNMIGRATE ")  # the new version never started: nothing to undo


def test_rollback_undoes_migrations_a_starting_panel_applied_while_it_was_being_stopped(site):
    site.toggle("slow_migrations")  # the new panel is still starting when the health wait gives up ...
    site.toggle("unhealthy_panel")
    assert site.upgrade("run", "--with-migrations", "--timeout", "0").returncode == 1
    site.toggle("unhealthy_panel", False)
    assert site.migrations() == OLD_MIGRATIONS
    site.forget_events()
    back = site.upgrade("rollback")  # ... and applies its migrations before it is stopped
    assert back.returncode == 0, back.both
    assert "the database changed while the containers were stopping" in back.stdout
    assert len(site.events("UNMIGRATE ")) == 3
    site.assert_old_version_runs()


# --- item 22: migrations are decided before anything is recorded, pulled or built -------------------
def test_run_without_with_migrations_refuses_before_it_records_or_builds_anything(site):
    refused = site.upgrade("run")
    assert refused.returncode == 2, refused.both
    assert "Nothing was changed" in refused.stderr
    assert "To do it:" in refused.stderr and "./scripts/upgrade.sh run --with-migrations" in refused.stderr
    # literally nothing: no state file, no dump, no tag, no pull, no build
    assert not site.state_file.exists() and not site.events() and not site.rollback_tags()
    assert not list(site.deploy.glob("upgrade-backup-*")) and not site.logs()
    assert not [call for call in site.read("calls.log") if " pull" in call or " build" in call]
    # the options of the refused command are part of the one it names
    refused = site.upgrade("run", "--no-pull", "--timeout", "60", "--from", site.old)
    assert f"./scripts/upgrade.sh run --with-migrations --from {site.old} --no-pull --timeout 60" in refused.stderr
    assert site.upgrade("run", "--with-migrations").returncode == 0


# --- finding 3: before the switch nothing is changed, the image names included ----------------------
def test_a_run_refused_after_the_build_points_the_image_names_back(site):
    """Migrations the plan could not know (here: a wrong --from) are still refused, after the build."""
    site.toggle("new_pg")  # the pull brings a newer database image, too
    refused = site.upgrade("run", "--from", site.new)
    assert refused.returncode == 2 and "refusing without --with-migrations" in refused.stderr
    assert "nothing was changed" in refused.stderr
    assert f"./scripts/upgrade.sh run --with-migrations --from {site.new}" in refused.stderr
    assert site.events("BUILD panel")
    names = site.images()
    assert names["mcpanel/panel:local"] == OLD_PANEL and names["mcpanel/agent:local"] == OLD_AGENT
    assert names["postgres:16-alpine"] == OLD_PG
    for name in ("mcpanel/panel:local", "mcpanel/agent:local", "postgres:16-alpine"):
        assert f"{name} points at the image it had before this run again" in refused.stderr
    assert site.containers()["panel"] == OLD_PANEL and tuple(sorted(site.containers())) == OLD_SERVICES
    assert site.status() == "started" and site.migrations() == OLD_MIGRATIONS


def test_a_run_that_ended_before_the_switch_is_continued_with_its_rollback_point(site):
    site.toggle("fail_build")
    failed = site.upgrade("run", "--with-migrations")
    assert failed.returncode == 1 and "the build failed; nothing was changed" in failed.stderr
    site.toggle("fail_build", False)
    assert site.events("BUILD agent")  # the agent image had been built and named already
    names = site.images()
    assert names["mcpanel/agent:local"] == OLD_AGENT and names["mcpanel/panel:local"] == OLD_PANEL
    assert site.status() == "started" and tuple(sorted(site.containers())) == OLD_SERVICES
    site.assert_rollback_point_is_the_old_version()
    tags = site.rollback_tags()
    first_dump = site.state()["BACKUP_SQL"]
    nothing = site.upgrade("rollback")
    assert nothing.returncode == 1 and "nothing to roll back" in nothing.stderr
    assert "./scripts/upgrade.sh run --with-migrations" in nothing.stderr

    # a second `git pull` overwrites ORIG_HEAD; the origin is the recorded one, not a guess
    newest = site.second_pull()
    site.next_second()
    done = site.upgrade("run")  # --with-migrations was given to the run it continues
    assert done.returncode == 0, done.both
    assert "did not finish" in done.stdout and "makes no new one" in done.stdout
    site.assert_new_version_runs(newest)
    assert site.status() == "done" and site.state()["TO_REV"] == [newest]
    site.assert_rollback_point_is_the_old_version()
    assert site.rollback_tags() == tags
    # the old version kept running in between: the dump is taken again
    assert site.state()["BACKUP_SQL"] != first_dump and len(site.state()["BACKUP_SQL"]) == 1

    back = site.upgrade("rollback")
    assert back.returncode == 0, back.both
    site.assert_old_version_runs()


def test_a_run_that_changed_nothing_is_not_continued_when_the_containers_were_changed_by_hand(site):
    site.toggle("fail_build")
    assert site.upgrade("run", "--with-migrations").returncode == 1
    site.toggle("fail_build", False)
    assert site.status() == "started"
    rows = [row.replace(OLD_PANEL, "sha256:made-by-hand") if "|panel|" in row else row for row in site.read("containers")]
    site.write("containers", rows)
    site.write("images", [*site.read("images"), "handmade:1|sha256:made-by-hand"])
    second = site.upgrade("run", "--with-migrations")
    assert second.returncode == 2 and DID_NOT_FINISH in second.stderr
    site.assert_rollback_point_is_the_old_version()


# --- finding 4: an origin the script cannot tell ------------------------------------------------------
def test_unknown_origin_is_refused_and_the_message_names_the_fix(site):
    site.second_pull()  # ORIG_HEAD now is a revision that was never deployed
    refused = site.upgrade("run", "--with-migrations")
    assert refused.returncode == 1 and not site.state_file.exists() and not site.events()
    message = refused.stderr
    assert "--from <revision>" in message and "io.mcpanel.revision" in message and "git reflog" in message
    assert "Nothing was changed" in message
    done = site.upgrade("run", "--with-migrations", "--from", site.old)
    assert done.returncode == 0, done.both
    site.assert_rollback_point_is_the_old_version()


def test_with_a_state_file_the_origin_is_never_guessed_from_the_layout(site):
    assert site.upgrade("run", "--with-migrations").returncode == 0
    # the panel image is replaced by hand: no label, not the image this script deployed
    site.write("containers", [re.sub(r"sha256:panel-new-\w+", "sha256:handmade", row) for row in site.read("containers")])
    site.write("images", [*site.read("images"), "handmade:1|sha256:handmade"])
    plan = site.upgrade("dry-run")
    assert "from:  unknown" in plan.stdout and "assumed" not in plan.stdout
    refused = site.upgrade("run", "--with-migrations")
    assert refused.returncode == 1 and "--from <revision>" in refused.stderr
    assert site.status() == "done"


# --- finding 2: migrations that cannot be undone ------------------------------------------------------
def test_rollback_stops_nothing_when_a_migration_cannot_be_undone_and_names_the_way_back(site):
    assert site.upgrade("run", "--with-migrations").returncode == 0
    dump = site.state()["BACKUP_SQL"][0]
    site.forget_events()
    site.toggle("irreversible_jobs")

    refused = site.upgrade("rollback")
    assert refused.returncode == 2, refused.both
    assert "migrations of 'jobs' cannot be undone" in refused.stderr and "IRREVERSIBLE" in refused.stderr
    assert "Nothing was changed" in refused.stderr
    assert not site.events("STOP ") and not site.events("UNMIGRATE ") and site.status() == "done"
    site.assert_new_version_runs()
    # the exact commands of the way back
    assert "docker compose stop agent panel" in refused.stderr
    assert f"./scripts/upgrade.sh restore-db {dump} --yes" in refused.stderr
    assert site.upgrade("rollback", "--dry-run").returncode == 2  # the dry run says so, too

    # ... followed to the letter
    busy = site.upgrade("restore-db", dump, "--yes")
    assert busy.returncode == 2 and "docker compose stop agent panel" in busy.stderr and not site.events("RESTORE")
    site.stop_services("agent", "panel")
    asked = site.upgrade("restore-db", dump)
    assert asked.returncode == 2 and "nothing was changed" in asked.stderr and "--yes" in asked.stderr
    assert not site.events("RESTORE")
    restored = site.upgrade("restore-db", dump, "--yes")
    assert restored.returncode == 0, restored.both
    assert len(site.events("RESTORE ")) == 1 and "keep-public" in site.events("RESTORE ")[0]
    assert "mcpanel_before_restore_" in restored.stdout and "dropdb" in restored.stdout
    assert site.migrations() == OLD_MIGRATIONS  # the dump is from before the upgrade
    # the upgrade is still the deployed version: the next step is the rollback
    assert "to finish going back" in restored.stdout and "      ./scripts/upgrade.sh rollback" in restored.stdout
    assert "==> Loading the dump" in site.logs()[-1].read_text()
    back = site.upgrade("rollback")
    assert back.returncode == 0, back.both
    site.assert_old_version_runs()
    assert not site.events("UNMIGRATE ")


def test_an_undo_that_fails_half_way_says_exactly_how_to_go_on(site):
    assert site.upgrade("run", "--with-migrations").returncode == 0
    dump = site.state()["BACKUP_SQL"][0]
    site.toggle("fail_unmigrate_jobs")  # the plan looks fine, the undo itself fails
    failed = site.upgrade("rollback")
    assert failed.returncode == 1
    assert site.events("UNMIGRATE ") == ["UNMIGRATE instances -> 0002_initial"]
    assert "BETWEEN the two" in failed.stderr
    assert "./scripts/upgrade.sh run --resume --no-pull" in failed.stderr
    assert f"./scripts/upgrade.sh restore-db {dump} --yes" in failed.stderr
    assert site.status() == "rolling-back" and site.head() == site.new
    site.toggle("fail_unmigrate_jobs", False)

    # a plain run must not take a half-rolled-back stack for a deployment
    refused = site.upgrade("run", "--with-migrations")
    assert refused.returncode == 2 and "the last rollback did not finish" in refused.stderr

    # forward again ...
    forward = site.upgrade("run", "--resume", "--no-pull")
    assert forward.returncode == 0, forward.both
    site.assert_new_version_runs()
    site.assert_rollback_point_is_the_old_version()
    assert site.state()["BACKUP_SQL"] == [dump]  # a dump of the half-undone schema would be no way back
    # ... and a rollback that works this time
    back = site.upgrade("rollback")
    assert back.returncode == 0, back.both
    site.assert_old_version_runs()


def test_restore_db_refuses_what_is_not_a_dump(site, tmp_path):
    other = tmp_path / "notes.sql"
    other.write_text("hello\n")
    assert site.upgrade("restore-db", str(other), "--yes").returncode == 1
    assert site.upgrade("restore-db").returncode == 1
    assert site.upgrade("restore-db", str(tmp_path / "missing.sql"), "--yes").returncode == 1
    assert not site.events("RESTORE")


def test_a_failed_restore_changes_nothing(site):
    assert site.upgrade("run", "--with-migrations").returncode == 0
    dump = site.state()["BACKUP_SQL"][0]
    site.stop_services("agent", "panel")
    site.toggle("fail_restore")
    failed = site.upgrade("restore-db", dump, "--yes")
    assert failed.returncode == 1 and "the restore failed" in failed.stderr
    assert site.migrations() == NEW_MIGRATIONS


# --- finding 5: the script after rollback's checkout, a closed terminal ------------------------------
def test_every_message_after_the_checkout_says_how_to_get_the_script_back(site):
    assert site.upgrade("run", "--with-migrations").returncode == 0
    site.toggle("fail_up")
    failed = site.upgrade("rollback")
    assert failed.returncode == 1 and "docker compose up failed" in failed.stderr
    assert site.head() == site.old and not (site.deploy / "scripts" / "upgrade.sh").exists()
    assert "gone from disk" in failed.stderr and "git checkout main" in failed.stderr
    assert site.status() == "rolling-back"
    site.toggle("fail_up", False)
    site.git("checkout", "-q", "main")
    refused = site.upgrade("run", "--with-migrations")
    assert refused.returncode == 2 and "the last rollback did not finish" in refused.stderr
    back = site.upgrade("rollback")
    assert back.returncode == 0, back.both
    site.assert_old_version_runs()


def test_the_comment_about_pruning_is_true():
    text = SCRIPT.read_text()
    assert "so no prune removes it" not in text
    assert "docker image prune -a" in text and "orphan images" in text


def test_no_loop_reads_the_state_file_through_a_pipe_it_may_leave_early():
    """`done < <(state_all KEY)` plus a break, return or die leaves a writer without a reader: with
    SIGPIPE ignored (a systemd unit) every remaining line prints "printf: write error: Broken pipe".
    Every run of this file has SIGPIPE ignored (Site.upgrade) and fails on that message."""
    code = [line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in code if "< <(state_all" in line]


def finish(process: subprocess.Popen[str], timeout: float = 60) -> str:
    output, _ = process.communicate(timeout=timeout)
    return output


# --- gate, defect 1a: a closed terminal after the switch began does not end the work ------------------
def test_a_closed_terminal_during_the_switch_does_not_stop_the_run(site):
    site.toggle("hang_up")
    with site.started("run", "--with-migrations") as process:
        site.wait_for_event("UP-WAIT", "the run never got to `docker compose up`")
        os.killpg(process.pid, signal.SIGHUP)  # the whole process group, like a terminal that closes
        time.sleep(0.5)
        assert process.poll() is None, "the hangup ended the run"
        assert site.status() == "switching"
        # ... and what it started is still there: `docker compose up` goes on
        site.toggle("release")
        output = finish(process)
    assert process.returncode == 0, output
    assert site.status() == "done" and "INTERRUPTED" not in site.state()
    site.assert_new_version_runs()
    site.assert_rollback_point_is_the_old_version()
    # docker compose ran where the hangup of the terminal does not get to it: in a session of its
    # own, and with the signal ignored as well
    assert site.events("UP-ENV") == ["UP-ENV own-session hup-ignored"]
    # how it ended is in the log, whatever became of the terminal
    (log,) = site.logs()
    text = log.read_text()
    assert "==> Done" in text and text.splitlines()[-1].endswith("exit status 0")
    assert not (site.deploy / "nohup.out").exists()


def test_without_setsid_nohup_keeps_docker_compose_alive(site):
    """A setsid that cannot wait (not the one of util-linux): the script falls back to nohup."""
    (site.bin / "setsid").write_text("#!/bin/sh\necho \"setsid: invalid option -- 'w'\" >&2\nexit 1\n")
    (site.bin / "setsid").chmod(0o755)
    site.toggle("hang_up")
    with site.started("run", "--with-migrations") as process:
        site.wait_for_event("UP-WAIT", "the run never got to `docker compose up`")
        os.killpg(process.pid, signal.SIGHUP)
        time.sleep(0.5)
        assert process.poll() is None, "the hangup ended the run"
        site.toggle("release")
        output = finish(process)
    assert process.returncode == 0, output
    assert site.status() == "done"
    site.assert_new_version_runs()
    assert site.events("UP-ENV") == ["UP-ENV callers-session hup-ignored"]
    assert not (site.deploy / "nohup.out").exists() and "nohup:" not in output


def test_a_closed_terminal_during_the_rollback_does_not_stop_it(site):
    assert site.upgrade("run", "--with-migrations").returncode == 0
    site.forget_events()
    site.toggle("hang_up")
    with site.started("rollback") as process:
        site.wait_for_event("UP-WAIT", "the rollback never got to `docker compose up`")
        os.killpg(process.pid, signal.SIGHUP)
        time.sleep(0.5)
        assert process.poll() is None, "the hangup ended the rollback"
        site.toggle("release")
        output = finish(process)
    assert process.returncode == 0, output
    assert site.status() == "rolled-back" and "INTERRUPTED" not in site.state()
    site.assert_old_version_runs()
    assert site.events("UP-ENV") == ["UP-ENV own-session hup-ignored"]
    assert len(site.events("UNMIGRATE ")) == 3
    assert "==> Rolled back" in site.logs()[-1].read_text()


def test_while_a_run_is_still_at_work_no_second_command_starts(site):
    """The run goes on without its terminal, so the owner may come back while it is still at work."""
    site.toggle("hang_up")
    with site.started("run", "--with-migrations") as process:
        site.wait_for_event("UP-WAIT", "the run never got to `docker compose up`")
        os.killpg(process.pid, signal.SIGHUP)
        before = site.state_file.read_text()
        (log,) = site.logs()
        status = site.upgrade("status")
        assert f"STILL AT WORK (pid {process.pid})" in status.stdout and f"tail -f {log}" in status.stdout
        assert "still at work on it" in status.stdout and "DID NOT FINISH" not in status.stdout
        for command in (("run", "--resume"), ("rollback",), ("run", "--no-build")):
            refused = site.upgrade(*command)
            assert refused.returncode == 2, refused.both
            assert "still at work" in refused.stderr and f"tail -f {log}" in refused.stderr
        assert "'run' would refuse now: another command" in site.upgrade("dry-run").stdout
        assert site.state_file.read_text() == before and site.logs() == [log]
        assert site.events("UP-WAIT") == ["UP-WAIT"]  # nothing else got to docker compose
        site.toggle("release")
        output = finish(process)
    assert process.returncode == 0, output
    site.assert_new_version_runs()
    status = site.upgrade("status")
    assert "STILL AT WORK" not in status.stdout and "exit status 0" in status.stdout


def test_a_kill_during_the_switch_is_recorded_as_unfinished(site):
    site.toggle("hang_up")
    with site.started("run", "--with-migrations") as process:
        site.wait_for_event("UP-WAIT", "the run never got to `docker compose up`")
        os.killpg(process.pid, signal.SIGTERM)
        # docker compose is in a session of its own: it finishes, and then the script ends
        site.toggle("release")
        output = finish(process)
    site.toggle("hang_up", False)
    assert process.returncode == 128 + signal.SIGTERM, output
    state = site.state()
    assert state["STATUS"] == ["switching"]
    assert len(state["INTERRUPTED"]) == 1 and state["INTERRUPTED"][0].startswith("SIGTERM at ")
    assert "Switching over" in state["INTERRUPTED"][0]
    site.assert_rollback_point_is_the_old_version()
    assert site.logs()[-1].read_text().splitlines()[-1].endswith("exit status 143")
    status = site.upgrade("status")
    assert "DID NOT FINISH" in status.stdout and "interrupted by SIGTERM" in status.stdout
    assert "exit status 143" in status.stdout
    refused = site.upgrade("run", "--with-migrations")
    assert refused.returncode == 2 and DID_NOT_FINISH in refused.stderr and "interrupted by SIGTERM" in refused.stderr
    resumed = site.upgrade("run", "--resume")
    assert resumed.returncode == 0, resumed.both
    assert "INTERRUPTED" not in site.state() and site.status() == "done"
    site.assert_rollback_point_is_the_old_version()


# --- gate, defect 1b: what an interrupted `docker compose up` left half-made --------------------------
def test_rollback_after_a_half_recreated_panel_works_at_the_first_attempt(site):
    site.half_recreate_the_panel()
    back = site.upgrade("rollback")
    assert back.returncode == 0, back.both
    site.assert_old_version_runs()
    assert site.status() == "rolled-back"
    assert f"removed {TEMP_PANEL} (created)" in back.stdout
    assert site.events("RM ") == [f"RM {TEMP_PANEL}"] and not site.events("UP-HALF")
    assert len(site.events("UP ")) == 1


def test_resume_after_a_half_recreated_panel_works_at_the_first_attempt(site):
    site.half_recreate_the_panel()
    resumed = site.upgrade("run", "--resume", "--timeout", "20")
    assert resumed.returncode == 0, resumed.both
    site.assert_new_version_runs()
    assert site.status() == "done"
    assert site.container_names() == ["mcpanel-agent-1", "mcpanel-panel-1", "mcpanel-postgres-1"]
    assert set(site.container_states().values()) == {"running"}
    assert f"RM {TEMP_PANEL}" in site.events("RM ") and not site.events("UP-HALF")
    site.assert_rollback_point_is_the_old_version()


def test_a_replacement_that_runs_under_its_temporary_name_gets_the_proper_name(site):
    """What two `up`s on top of a half-recreated panel leave: the replacement runs, still under its temporary name."""
    site.half_recreate_the_panel()
    rows = []
    for row in site.read("containers"):
        fields = row.split("|")
        if fields[1] == "panel" and len(fields) > 7:
            fields[2], fields[6] = "running", "healthy"
        rows.append("|".join(fields))
    site.write("containers", rows)  # the stopped original is still there
    resumed = site.upgrade("run", "--resume")
    assert resumed.returncode == 0, resumed.both
    assert site.events("RM ")[0] == "RM panel" and site.events("RENAME ") == [f"RENAME {TEMP_PANEL} -> mcpanel-panel-1"]
    assert "removed the stopped container mcpanel-panel-1 (exited)" in resumed.stdout
    assert f"renamed {TEMP_PANEL} to mcpanel-panel-1" in resumed.stdout
    assert site.container_names() == ["mcpanel-agent-1", "mcpanel-panel-1", "mcpanel-postgres-1"]
    site.assert_new_version_runs()


# --- gate, defect 1c: `up` ends without an error and a service is not started --------------------------
def test_up_that_leaves_the_panel_unstarted_is_tried_once_more(site):
    site.write("panel_not_started", ["1"])
    done = site.upgrade("run", "--with-migrations")
    assert done.returncode == 0, done.both
    assert "did not start: panel(created). Once more:" in done.stdout
    assert len(site.events("UP ")) == 2
    site.assert_new_version_runs()
    assert site.status() == "done"


def test_up_that_leaves_the_panel_unstarted_twice_fails_at_once_and_says_so(site):
    site.write("panel_not_started", ["always"])
    began = time.monotonic()
    failed = site.upgrade("run", "--with-migrations", timeout=60)  # the wait for healthy would take 300 s
    assert time.monotonic() - began < 30
    assert failed.returncode == 1, failed.both
    message = " ".join(failed.stderr.split())
    assert "docker compose up ended without an error, twice, and still did not start: panel(created)" in message
    assert "./scripts/upgrade.sh rollback" in message and "./scripts/upgrade.sh run --resume" in message
    assert "not healthy" not in failed.both and "Waiting for the services" not in failed.stdout
    assert site.status() == "switching" and len(site.events("UP ")) == 2
    (site.fake / "panel_not_started").unlink()
    resumed = site.upgrade("run", "--resume")
    assert resumed.returncode == 0, resumed.both
    site.assert_new_version_runs()


def test_the_summary_names_the_images_of_the_running_containers(site):
    assert site.upgrade("run", "--with-migrations").returncode == 0
    agent = site.containers()["agent"]
    site.next_second()
    # the next build gives both images new ids, and Compose leaves the agent container as it is
    site.toggle("rebuild_ids")
    site.toggle("keep_agent")
    again = site.upgrade("run", "--no-pull")
    assert again.returncode == 0, again.both
    assert site.containers()["agent"] == agent and site.images()["mcpanel/agent:local"] != agent
    summary = again.stdout[again.stdout.index("==> Done") :]
    assert "mcpanel/agent:local: unchanged (" in summary  # the container that runs, not what the name points at
    assert "mcpanel/panel:local: panel-new-" in summary and "mcpanel/panel:local: unchanged" not in summary
    assert site.state()["DEPLOYED_PANEL_IMAGE"] == [site.containers()["panel"]]


# --- gate, defect 2: restore-db says what fits the state of the last run -------------------------------
def test_restore_db_after_a_finished_rollback_names_the_checkout_and_the_command_that_fit(site):
    assert site.upgrade("run", "--with-migrations").returncode == 0
    dump = site.state()["BACKUP_SQL"][0]
    assert site.upgrade("rollback").returncode == 0
    site.git("checkout", "-q", "main")  # the script is back: the tree is the new version, the old one runs
    site.stop_services(*[name for name in OLD_SERVICES if name != "postgres"])
    restored = site.upgrade("restore-db", dump, "--yes")
    assert restored.returncode == 0, restored.both
    words = " ".join(restored.stdout.split())
    assert "The last upgrade was rolled back" in words and f"({site.old[:12]})" in words
    # not the rollback (it is done), and not `docker compose up -d` with the compose file of the new version
    assert "upgrade.sh rollback" not in words
    assert "do NOT run 'docker compose up -d' in this checkout" in words
    command = f"cd {site.root} && git checkout --detach {site.old} && cd deploy && docker compose up -d"
    assert f"      {command}\n" in restored.stdout
    # ... followed to the letter
    followed = subprocess.run(["bash", "-c", command], env=site.env, capture_output=True, text=True, check=False)
    assert followed.returncode == 0, followed.stderr
    site.assert_old_version_runs()
    assert set(site.container_states().values()) == {"running"}


def test_restore_db_after_a_run_that_did_not_finish_names_both_ways_out(site):
    unhealthy_run(site)
    dump = site.state()["BACKUP_SQL"][0]
    site.stop_services("agent", "panel")
    restored = site.upgrade("restore-db", dump, "--yes")
    assert restored.returncode == 0, restored.both
    assert "do NOT run 'docker compose up -d' now" in restored.stdout
    assert "./scripts/upgrade.sh rollback " in restored.stdout and "./scripts/upgrade.sh run --resume " in restored.stdout
    back = site.upgrade("rollback")
    assert back.returncode == 0, back.both
    site.assert_old_version_runs()


# --- gate, defect 4: status never reads a container that does not run ----------------------------------
def test_status_does_not_take_a_container_that_was_only_made_for_the_running_panel(site):
    site.half_recreate_the_panel()
    status = site.upgrade("status")
    assert status.returncode == 0, status.both
    assert "Deployed:     the panel is not running" in status.stdout
    assert "label of the running panel image" not in status.stdout
    assert "DID NOT FINISH" in status.stdout and f"{TEMP_PANEL} created" in " ".join(status.stdout.split())


def test_status_names_the_deployed_revision_after_a_run_that_ended_before_the_switch(site):
    site.toggle("fail_build")
    assert site.upgrade("run", "--with-migrations").returncode == 1
    status = site.upgrade("status")
    line = next(line for line in status.stdout.splitlines() if line.startswith("Deployed:"))
    assert site.old[:7] in line and "by the run that ended before the switch" in line and "unknown" not in line
    assert "do not run 'docker compose up -d' here" in " ".join(status.stdout.split())


def test_status_of_a_stopped_stack_says_that_the_panel_is_not_running(site):
    assert site.upgrade("run", "--with-migrations").returncode == 0
    site.stop_services("agent", "panel")
    status = site.upgrade("status")
    line = next(line for line in status.stdout.splitlines() if line.startswith("Deployed:"))
    assert "the panel is not running" in line and site.new[:7] in line and "label of the running" not in line


# --- gate, defect 5: an interruption while output goes into a file is still said -------------------------
def test_an_interruption_during_the_migration_check_is_said_and_logged(site):
    site.toggle("hang_check")
    with site.started("run", "--with-migrations") as process:
        site.wait_for_event("CHECK-WAIT", "the run never got to the migration check")
        os.killpg(process.pid, signal.SIGHUP)  # before the switch a closed terminal still ends the run
        output = finish(process, timeout=30)
    site.toggle("hang_check", False)
    assert process.returncode == 128 + signal.SIGHUP, output
    said = "upgrade: interrupted by SIGHUP during: Database migrations of the new version. What is left to do: ./scripts/upgrade.sh status"
    assert said in output
    for name in ("mcpanel/panel:local", "mcpanel/agent:local"):
        assert f"upgrade: {name} points at the image it had before this run again" in output
    assert site.images()["mcpanel/panel:local"] == OLD_PANEL and site.status() == "started"
    assert site.state()["INTERRUPTED"][0].startswith("SIGHUP at ")
    text = site.logs()[-1].read_text()
    assert said in text and text.splitlines()[-1].endswith("exit status 129")
    site.assert_old_version_runs_but_tree_is_new()
    done = site.upgrade("run")  # goes on with the recorded rollback point
    assert done.returncode == 0, done.both
    site.assert_new_version_runs()


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="needs shellcheck (the CI job `docker` runs it as well)")
def test_shellcheck_has_nothing_to_say_about_the_deployment_scripts():
    scripts = [
        REPO / "deploy" / "scripts" / "generate-secrets.sh",
        SCRIPT,
        REPO / "backend" / "container" / "entrypoint.sh",
        REPO / ".github" / "scripts" / "panel-acceptance.sh",
        REPO / ".github" / "scripts" / "upgrade-rehearsal.sh",
        REPO / "deploy" / "unraid" / "stopping_docker",
    ]
    result = subprocess.run(["shellcheck", *map(str, scripts)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout
    hook = subprocess.run(
        ["shellcheck", "--exclude=SC2016", str(REPO / "deploy" / "unraid" / "install-hook.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert hook.returncode == 0, hook.stdout


# --- finding 6: exited server containers ----------------------------------------------------------------
SERVERS = ["s1|exited", "s2|exited", "s3|running", "s4|created", "s5|exited", "s6|exited", "s7|exited", "s8|dead"]


def test_dry_run_and_run_say_that_exited_server_containers_will_be_removed(site):
    site.write("servers", SERVERS)
    for result in (site.upgrade("dry-run"), site.upgrade("run", "--with-migrations")):
        assert result.returncode == 0, result.both
        text = " ".join(result.stdout.split())
        assert "this host has 7 exited Minecraft server containers." in text
        assert "the panel removes these 7 containers." in text and "last 2,000 console lines" in text
        assert "started from the panel" in text
        assert "MCPANEL_RETIRE_STOPPED_CONTAINERS=false" in text and "BEFORE the run" in text
        assert "manage.py retire_stopped_containers --dry-run" in text


@pytest.mark.parametrize(
    ("env_line", "notice", "kept"),
    [
        ("MCPANEL_RETIRE_STOPPED_CONTAINERS=", True, False),  # the line of .env.example: the default, on
        ("MCPANEL_RETIRE_STOPPED_CONTAINERS=false", False, True),
        ('MCPANEL_RETIRE_STOPPED_CONTAINERS="False"', False, True),
        ("MCPANEL_RETIRE_STOPPED_CONTAINERS=true", False, False),  # decided: nothing to explain
    ],
)
def test_the_notice_depends_on_the_setting_in_env(site, env_line, notice, kept):
    site.write("servers", SERVERS)
    with (site.deploy / ".env").open("a") as fh:
        fh.write(env_line + "\n")
    out = site.upgrade("dry-run").stdout
    assert ("the panel removes these 7 containers." in out) is notice
    assert ("7 exited server containers are kept on this host" in out) is kept


def test_the_notice_about_one_exited_server_container_is_a_sentence(site):
    site.write("servers", ["s1|exited", "s3|running"])
    text = " ".join(site.upgrade("dry-run").stdout.split())
    assert "this host has 1 exited Minecraft server container. A stopped server" in text
    assert "the panel removes this container. World data" in text and "container(s)" not in text
    with (site.deploy / ".env").open("a") as fh:
        fh.write("MCPANEL_RETIRE_STOPPED_CONTAINERS=false\n")
    assert "1 exited server container is kept on this host" in site.upgrade("dry-run").stdout


def test_no_notice_without_exited_server_containers(site):
    site.write("servers", ["s3|running"])
    assert "Stopped servers" not in site.upgrade("dry-run").stdout


# --- rollback to the release before multi-game: servers of other games ---------------------------
class GamesSite(Site):
    """The old revision is the last one before multi-game (no `game` column, player names of 16
    characters); the new one adds instances 0005-0007 and metrics 0002."""

    old_migrations = {**OLD_MIGRATIONS, "instances": "0004_schedule_deferred_at"}
    upgrade_migrations = {"instances": "0007_restore_other_games", "metrics": "0002_games_phase2"}

    def servers(self) -> dict[str, list[str]]:
        """id -> [name, game, port, archived]"""
        return {line.split("|")[0]: line.split("|")[1:] for line in self.read("servers_db")}


GAMES_UNDO = "--undo instances=0004_schedule_deferred_at --undo metrics=0001_initial"
MC_AND_FACTORIO = ["mc1|Survival|minecraft|25600|no", "fa1|Factory|factorio|34197|no"]


@pytest.fixture
def games_site(tmp_path: Path) -> GamesSite:
    site = GamesSite(tmp_path)
    assert site.upgrade("run", "--with-migrations").returncode == 0
    site.assert_new_version_runs()
    site.forget_events()
    return site


def assert_nothing_was_changed_by_the_rollback(site: GamesSite) -> None:
    site.assert_new_version_runs()
    assert site.status() == "done" and not site.events("STOP ") and not site.events("UNMIGRATE ")
    assert not site.events("ARCHIVE ") and not (site.fake / "side_games").exists()


def test_rollback_refuses_while_servers_of_other_games_exist_and_lists_them(games_site):
    site = games_site
    site.write("servers_db", MC_AND_FACTORIO)
    logs = site.logs()
    for args in (("rollback", "--dry-run"), ("rollback",)):
        result = site.upgrade(*args)
        assert result.returncode == 2, result.both
        assert "fa1  'Factory'  factorio" in result.stdout and "mc1" not in result.stdout
        words = " ".join(result.stderr.split())
        assert "knows only Minecraft servers" in words and "Nothing was changed. Choose one:" in words
        assert "keep the new version" in words and "./scripts/upgrade.sh rollback --archive-other-games" in words
        assert "./scripts/upgrade.sh restore-db " in words  # the database dump way back
        assert_nothing_was_changed_by_the_rollback(site)
        # asked before anything was stopped (and before the plan of the undo), with the targets of the rollback
        assert site.events("GAMES-CHECK ") == [f"GAMES-CHECK {GAMES_UNDO}"]
        assert site.events()[-1].startswith("GAMES-CHECK ") and not site.events("DUMP")
        site.forget_events()
    assert site.logs() == logs  # a refusal writes no log
    assert site.servers()["fa1"] == ["Factory", "factorio", "34197", "no"]


def test_rollback_refuses_when_two_servers_share_a_port_number(games_site):
    site = games_site
    # Minecraft's TCP 25600 and Factorio's UDP 25600: allowed now, refused by the old unique index
    site.write("servers_db", ["mc1|Survival|minecraft|25600|no", "fa1|Factory|factorio|25600|no"])
    result = site.upgrade("rollback")
    assert result.returncode == 2, result.both
    assert "Port numbers several servers of one node share" in result.stdout and "  port 25600" in result.stdout
    assert "port numbers that two servers share make undoing the migrations fail" in " ".join(result.stderr.split())
    assert_nothing_was_changed_by_the_rollback(site)


def test_a_job_on_a_server_of_another_game_blocks_the_rollback_even_with_archive_other_games(games_site):
    site = games_site
    site.write("servers_db", MC_AND_FACTORIO)
    site.toggle("games_busy")
    result = site.upgrade("rollback", "--archive-other-games", "--force")
    assert result.returncode == 2, result.both
    assert "an operation is in progress" in result.stdout and "the rollback cannot go on now" in result.stderr
    assert_nothing_was_changed_by_the_rollback(site)


def test_archive_other_games_then_rollback_then_run_puts_them_back(games_site):
    site = games_site
    site.write("servers_db", ["mc1|Survival|minecraft|25600|no", "fa1|Factory|factorio|25600|no", "te1|Terra|terraria|7777|no"])
    plan = site.upgrade("rollback", "--archive-other-games", "--dry-run")
    assert plan.returncode == 0, plan.both
    assert "'rollback' would stop these servers through the running panel" in plan.stdout
    assert plan.stdout.rstrip().endswith("To do it: ./scripts/upgrade.sh rollback --archive-other-games")
    assert_nothing_was_changed_by_the_rollback(site)
    site.forget_events()

    back = site.upgrade("rollback", "--archive-other-games")
    assert back.returncode == 0, back.both
    site.assert_old_version_runs()
    events = site.events()
    first_stop = min(i for i, e in enumerate(events) if e.startswith("STOP "))
    # archived through the running panel, before anything was stopped, then the undo
    assert events.index(f"GAMES-ARCHIVE --archive {GAMES_UNDO}") < events.index("ARCHIVE fa1") < first_stop
    assert events.index("ARCHIVE te1") < first_stop < events.index("UNMIGRATE instances -> 0004_schedule_deferred_at")
    assert not site.events("LOST ")  # without the archive step the shim records the lost game here
    assert site.servers() == {
        "mc1": ["Survival", "-", "25600", "no"],
        "fa1": ["Factory", "-", "65536", "yes"],  # (the rollback port: Survival has 25600)
        "te1": ["Terra", "-", "7777", "yes"],
    }
    assert site.read("side_games") == ["fa1|factorio|25600", "te1|terraria|7777"]
    log = site.logs()[-1].read_text()
    assert "==> Archiving the servers of other games (through the running panel)" in log and "'Factory': archived" in log
    assert "Do not unarchive them in this version" in " ".join(back.stdout.split())

    # upgrade again: the new panel's migration 0007 puts them back, still archived
    site.git("checkout", "-q", "main")
    site.next_second()
    again = site.upgrade("run", "--with-migrations")
    assert again.returncode == 0, again.both
    site.assert_new_version_runs()
    assert site.events("RESTORE-OTHER-GAMES ") == ["RESTORE-OTHER-GAMES 2"]
    assert site.servers() == {
        "mc1": ["Survival", "minecraft", "25600", "no"],
        "fa1": ["Factory", "factorio", "25600", "yes"],
        "te1": ["Terra", "terraria", "7777", "yes"],
    }
    assert not (site.fake / "side_games").exists()


def test_archiving_that_fails_stops_nothing_and_says_how_to_go_on(games_site):
    site = games_site
    site.write("servers_db", [*MC_AND_FACTORIO, "te1|Terra|terraria|7777|no"])
    site.toggle("fail_archive")
    result = site.upgrade("rollback", "--archive-other-games")
    assert result.returncode == 1, result.both
    assert site.events("ARCHIVE ") == ["ARCHIVE fa1"] and "the agent did not answer" in result.stdout
    words = " ".join(result.stderr.split())
    assert "Nothing was stopped and no migration was undone: the upgraded version keeps running" in words
    assert "./scripts/upgrade.sh rollback --archive-other-games' again" in words
    site.assert_new_version_runs()
    assert site.status() == "done" and not site.events("STOP ") and not site.events("UNMIGRATE ")
    # once the problem is gone the rollback goes on with what is left
    site.toggle("fail_archive", False)
    site.forget_events()
    back = site.upgrade("rollback", "--archive-other-games")
    assert back.returncode == 0, back.both
    site.assert_old_version_runs()
    assert not site.events("LOST ") and site.read("side_games") == ["fa1|factorio|34197", "te1|terraria|7777"]


def test_a_rollback_without_servers_of_other_games_asks_and_goes_on(games_site):
    site = games_site
    site.write("servers_db", ["mc1|Survival|minecraft|25600|no"])
    back = site.upgrade("rollback")
    assert back.returncode == 0, back.both
    site.assert_old_version_runs()
    assert "servers of other games: none" in back.stdout and site.events("GAMES-CHECK ")
    assert not site.events("GAMES-ARCHIVE ")


# --- the rollback's own dump, and the guard of migrations instances 0005/0006 ---------------------
def rollback_dump(site: Site) -> str:
    (dump,) = site.state()["ROLLBACK_SQL"]
    return dump


def test_rollback_dumps_the_database_first_and_every_failure_names_that_dump(site):
    assert site.upgrade("run", "--with-migrations").returncode == 0
    (before_upgrade,) = site.state()["BACKUP_SQL"]
    site.forget_events()
    site.toggle("fail_unmigrate_jobs")
    failed = site.upgrade("rollback")
    assert failed.returncode == 1, failed.both
    dump = rollback_dump(site)
    assert dump != before_upgrade and Path(before_upgrade).exists() and site.state()["BACKUP_SQL"] == [before_upgrade]
    assert stat.S_IMODE(Path(dump).stat().st_mode) == 0o600 and stat.S_IMODE(Path(dump[:-4] + ".env").stat().st_mode) == 0o600
    events = site.events()
    assert events.index("DUMP") < min(i for i, e in enumerate(events) if e.startswith("STOP "))
    # the way back the message names first is that dump: nothing written since the upgrade is lost
    words = " ".join(failed.stderr.split())
    assert words.index(f"./scripts/upgrade.sh restore-db {dump} --yes") < words.index(f"restore-db {before_upgrade} --yes")
    site.toggle("fail_unmigrate_jobs", False)

    # a failure that has no message of its own about the way back ends with it too
    site.next_second()
    site.toggle("fail_up")
    again = site.upgrade("rollback")
    assert again.returncode == 1, again.both
    newer = rollback_dump(site)
    assert newer != dump and f"./scripts/upgrade.sh restore-db {newer} --yes" in " ".join(again.stderr.split())
    site.toggle("fail_up", False)

    # --no-backup: no dump, and it says what that means
    site.git("checkout", "-q", "main")  # (the failed rollback had checked out the old revision already)
    site.forget_events()
    plain = site.upgrade("rollback", "--no-backup")
    assert plain.returncode == 0, plain.both
    assert not site.events("DUMP") and "skipped (--no-backup)" in plain.stdout and "has no way back but" in plain.stdout
    site.assert_old_version_runs()


def test_the_plan_of_the_undo_refuses_while_servers_of_other_games_are_not_recorded(games_site):
    site = games_site
    site.write("servers_db", MC_AND_FACTORIO)
    site.toggle("games_check_blind")  # even when the check misses them, migrations 0005/0006 do not
    refused = site.upgrade("rollback")
    assert refused.returncode == 2, refused.both
    words = " ".join(refused.stderr.split())
    assert "migrations of 'instances' cannot be undone (Raw Python operation -> IRREVERSIBLE (other games))" in words
    assert "not archived and recorded: ./scripts/upgrade.sh rollback --archive-other-games" in words
    assert_nothing_was_changed_by_the_rollback(site)


def test_an_archive_that_misses_a_server_is_caught_by_the_plan_before_anything_is_stopped(games_site):
    site = games_site
    site.write("servers_db", [*MC_AND_FACTORIO, "te1|Terra|terraria|7777|no"])
    site.toggle("archive_misses")
    refused = site.upgrade("rollback", "--archive-other-games")
    assert refused.returncode == 2, refused.both
    assert site.events("ARCHIVE ") == ["ARCHIVE fa1", "ARCHIVE te1"]
    events = site.events()
    assert max(i for i, e in enumerate(events) if e.startswith("PLAN instances")) > events.index("ARCHIVE te1")
    words = " ".join(refused.stderr.split())
    assert "IRREVERSIBLE (other games)" in words and "Nothing was stopped and no migration was undone" in words
    assert f"./scripts/upgrade.sh restore-db {rollback_dump(site)} --yes" in words
    site.assert_new_version_runs()
    assert site.status() == "done" and not site.events("STOP ") and not site.events("UNMIGRATE ")
