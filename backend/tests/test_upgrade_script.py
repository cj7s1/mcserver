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

import os
import re
import shutil
import signal
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


def compose_file(services: tuple[str, ...]) -> str:
    return "name: mcpanel\nservices:\n" + "".join(f"  {name}:\n    image: x\n" for name in services) + "volumes: {}\n"


class Site:
    """A deployment: the git checkout with deploy/.env, and the world the stand-in docker keeps."""

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
        (self.root / ".gitignore").write_text("deploy/.env\ndeploy/*.env\ndeploy/*.sql\ndeploy/*.sql.part\n")
        (self.deploy / "docker-compose.yml").write_text(compose_file(OLD_SERVICES))
        for app, name in OLD_MIGRATIONS.items():
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
        for app, name in UPGRADE_MIGRATIONS.items():
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
        old = [f"{app} {name}" for app, name in sorted(OLD_MIGRATIONS.items())]
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
        assert self.migrations() == OLD_MIGRATIONS
        assert self.head() == self.old

    def assert_new_version_runs(self, revision: str | None = None) -> None:
        running = self.containers()
        assert tuple(sorted(running)) == NEW_SERVICES
        assert running["panel"] == f"sha256:panel-new-{(revision or self.new)[:12]}"
        assert self.migrations() == NEW_MIGRATIONS

    def assert_rollback_point_is_the_old_version(self) -> None:
        assert self.state()["FROM_REV"] == [self.old]
        recorded = self.recorded_images()
        assert recorded["mcpanel/panel:local"] == OLD_PANEL and recorded["mcpanel/agent:local"] == OLD_AGENT
        assert recorded["postgres:16-alpine"] == OLD_PG and recorded["redis:7-alpine"] == OLD_REDIS
        tagged = self.images()
        for entry in self.state()["IMAGE"]:
            _name, image_id, tag = entry.split("|")
            assert tagged.get(tag) == image_id, f"the rollback tag {tag} is gone or moved"
        assert [f"{app} {name}" for app, name in sorted(OLD_MIGRATIONS.items())] == self.state()["MIGRATION"]


@pytest.fixture
def site(tmp_path: Path) -> Site:
    return Site(tmp_path)


# --- the plain way: run, rollback, run again ------------------------------------------------------
def test_run_then_rollback_then_run_again(site):
    plan = site.upgrade("dry-run")
    assert plan.returncode == 0 and not site.state_file.exists()
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
    site.forget_events()

    back = site.upgrade("rollback")
    assert back.returncode == 0, back.both
    site.assert_old_version_runs()
    assert site.status() == "rolled-back"
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

    site.git("checkout", "-q", "main")
    assert "./scripts/upgrade.sh run --with-migrations" in site.upgrade("rollback").stderr  # (rolled back already)
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
    assert [event.split()[0] for event in site.events()] == ["UP"]  # no pull, no build, no dump, no tag
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
    assert not list(site.deploy.glob("upgrade-backup-*"))
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


@pytest.mark.parametrize("signal_name", ["SIGHUP", "SIGTERM"])
def test_a_closed_terminal_during_the_switch_is_recorded_as_unfinished(site, signal_name):
    site.toggle("hang_up")
    process = subprocess.Popen(
        ["./scripts/upgrade.sh", "run", "--with-migrations"],
        cwd=site.deploy,
        env=site.env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60
        while "UP-WAIT" not in site.events() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "UP-WAIT" in site.events(), "the run never got to `docker compose up`"
        os.killpg(process.pid, getattr(signal, signal_name))  # the whole session, like a terminal that closes
        output, _ = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
    site.toggle("hang_up", False)
    assert process.returncode == 128 + getattr(signal, signal_name), output
    state = site.state()
    assert state["STATUS"] == ["switching"]
    assert len(state["INTERRUPTED"]) == 1 and state["INTERRUPTED"][0].startswith(f"{signal_name} at ")
    assert "Switching over" in state["INTERRUPTED"][0]
    site.assert_rollback_point_is_the_old_version()
    status = site.upgrade("status")
    assert "DID NOT FINISH" in status.stdout and f"interrupted by {signal_name}" in status.stdout
    refused = site.upgrade("run", "--with-migrations")
    assert refused.returncode == 2 and DID_NOT_FINISH in refused.stderr and f"interrupted by {signal_name}" in refused.stderr
    resumed = site.upgrade("run", "--resume")
    assert resumed.returncode == 0, resumed.both
    assert "INTERRUPTED" not in site.state() and site.status() == "done"
    site.assert_rollback_point_is_the_old_version()


# --- finding 6: exited server containers ----------------------------------------------------------------
SERVERS = ["s1|exited", "s2|exited", "s3|running", "s4|created", "s5|exited", "s6|exited", "s7|exited", "s8|dead"]


def test_dry_run_and_run_say_that_exited_server_containers_will_be_removed(site):
    site.write("servers", SERVERS)
    for result in (site.upgrade("dry-run"), site.upgrade("run", "--with-migrations")):
        assert result.returncode == 0, result.both
        text = " ".join(result.stdout.split())
        assert "this host has 7 exited Minecraft server container(s)" in text
        assert "the panel removes these 7 container(s)" in text and "last 2,000 console lines" in text
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
    assert ("the panel removes these 7 container(s)" in out) is notice
    assert ("7 exited server container(s) on this host are kept" in out) is kept


def test_no_notice_without_exited_server_containers(site):
    site.write("servers", ["s3|running"])
    assert "Stopped servers" not in site.upgrade("dry-run").stdout
