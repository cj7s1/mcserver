"""deploy/scripts/upgrade.sh: what it tells the owner at the end of a run and of a rollback (review R-9, R-12).

* After `run`: everyone signs in once when the deployed version had no session binding (the panel's
  sessions belong to the browser tab since then), and tabs left open must be reloaded.
* After `rollback` to a version without per-server networks: its agent is not attached to them, so a
  server on one runs on, but console, player list and the save before a stop do not work until it was
  stopped and started once.

Text only; the site with the stand-in docker is the one of tests/test_upgrade_script.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .test_upgrade_script import Site, pytestmark  # noqa: F401 (pytestmark: the same skip)

SIGN_IN = "Everyone signs in once now: since this version a session belongs to the browser tab that signed in."
RELOAD = "Panel tabs that were open during the upgrade: reload them"
NETWORKS = "Servers on a network of their own (zz_mcpanel_i_...): the agent of this version is not attached to"


class SiteWithBoth(Site):
    """An old revision that already has session binding and per-server networks."""

    def _build_checkout(self) -> None:
        for path in ("agent/mcagent/pernet.py", "backend/apps/core/session_binding.py"):
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text("# here already\n")
        super()._build_checkout()


def words(text: str) -> str:
    return " ".join(text.split())


def test_run_says_sign_in_once_and_reload_and_rollback_names_the_server_networks(tmp_path: Path):
    site = Site(tmp_path)
    done = site.upgrade("run", "--with-migrations")
    assert done.returncode == 0, done.both
    lines = done.stdout.rstrip().splitlines()
    assert lines[-2] == SIGN_IN and lines[-1].startswith(RELOAD)
    assert SIGN_IN in site.logs()[-1].read_text()  # and in the log

    back = site.upgrade("rollback")
    assert back.returncode == 0, back.both
    text = words(back.stdout)
    assert NETWORKS in back.stdout
    assert (
        "Such a server keeps running and players can join it, but its console, its player list and the save "
        "before a stop do not work until it was stopped and started once" in text
    )
    assert "A Minecraft server still saves by itself when it is stopped." in text
    # before the notes about the checkout, after the list of containers
    assert back.stdout.index("==> Rolled back") < back.stdout.index(NETWORKS) < back.stdout.index("The source tree")


def test_no_sign_in_or_network_note_when_the_other_version_has_them(tmp_path: Path):
    site = SiteWithBoth(tmp_path)
    done = site.upgrade("run", "--with-migrations")
    assert done.returncode == 0, done.both
    assert SIGN_IN not in done.stdout and done.stdout.rstrip().splitlines()[-1].startswith(RELOAD)
    back = site.upgrade("rollback")
    assert back.returncode == 0, back.both
    assert NETWORKS not in back.stdout


@pytest.mark.parametrize("doc", ["DEPLOYMENT.md"])
def test_the_upgrade_notes_say_the_same(doc: str):
    text = words((Path(__file__).resolve().parents[2] / doc).read_text())
    assert "reload" in text.lower() and "everyone signs in once" in text.lower()
    assert "console, player list and the save before a stop do not work until" in text
