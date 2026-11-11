"""Satisfactory's files get the runtime uid:gid (real-stack run 2026-11-10, item 20).

wolveix's init.sh only adds ``steam`` to a group that already exists in the image (gid 100,
``users``, on Unraid), so the game keeps the primary group 1000 and every file written during a run
is ``99:1000`` until the next start's ``chown -R``. A PGID the image lacks (Terraria's way) would
give ``99:<that gid>``, still not the runtime gid, so the agent gives the server folder the runtime
owner itself: after every stop and before every backup (``fix_ownership``), never following links.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from mcagent import docker_ops, drivers, main, profiles, safefs
from mcagent.profiles import satisfactory
from mcagent.safefs import SafeRoot

IID = uuid.UUID("0123abcd-0000-4000-8000-0000000a0e20")
OTHER_GID = 4242  # a group the files do not have: every entry needs the fix


@pytest.fixture
def chowns(monkeypatch):
    """Record the ownership changes instead of making them (the tests do not run as root)."""
    calls: list = []

    def chown(name, uid, gid, *, dir_fd=None, follow_symlinks=True):
        calls.append((os.readlink(f"/proc/self/fd/{dir_fd}") + "/" + name, uid, gid, follow_symlinks))

    def fchown(fd, uid, gid):
        calls.append((os.readlink(f"/proc/self/fd/{fd}"), uid, gid, False))

    monkeypatch.setattr(safefs.os, "chown", chown)
    monkeypatch.setattr(safefs.os, "fchown", fchown)
    return calls


@pytest.fixture
def folder(agent_settings, tmp_path):
    root = agent_settings.instances_dir / str(IID)
    (root / "saved" / "server").mkdir(parents=True)
    (root / "saved" / "server" / "Ficsit_mcpanel.sav").write_bytes(b"save")
    (root / "gamefiles" / "FactoryGame").mkdir(parents=True)
    (root / "gamefiles" / "FactoryGame" / "big.pak").write_bytes(b"pak")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "host-file").write_text("not ours")
    (root / "link-out").symlink_to(outside)
    return root


def test_chown_tree_fixes_everything_below_without_following_links(agent_settings, folder, chowns, tmp_path):
    uid = os.getuid()
    changed = SafeRoot(folder).chown_tree(uid, OTHER_GID)
    paths = {p: follow for p, _u, _g, follow in chowns}
    assert str(folder) in paths and str(folder / "saved/server/Ficsit_mcpanel.sav") in paths
    assert str(folder / "gamefiles/FactoryGame/big.pak") in paths
    assert paths[str(folder / "link-out")] is False  # the link itself, never its target
    assert not any("host-file" in p or str(tmp_path / "outside") == p for p in paths)
    assert all(u == uid and g == OTHER_GID for _p, u, g, _f in chowns) and changed == len(chowns)
    chowns.clear()
    SafeRoot(folder).chown_tree(uid, OTHER_GID, skip=("gamefiles",))
    assert not any("/gamefiles" in p for p, *_ in chowns)
    chowns.clear()
    # entries that already have the owner are left alone
    assert SafeRoot(folder).chown_tree(uid, os.getgid()) == 0 and chowns == []


def test_fix_ownership_never_raises(agent_settings, folder, monkeypatch):
    def refuse(*_a, **_k):
        raise PermissionError("not root")

    monkeypatch.setattr(safefs.os, "fchown", refuse)
    agent_settings.runtime_gid = OTHER_GID
    warning = docker_ops.fix_ownership(agent_settings, IID)
    assert warning is not None and "could not be given the owner" in warning


def test_only_satisfactory_and_project_zomboid_ask_for_it():
    # PZ's image runs the game as root (no privilege drop): files written during a run are 0:0
    assert satisfactory.PROFILE.fix_ownership is True
    assert [p.game for p in profiles.all_profiles() if p.fix_ownership] == ["satisfactory", "project-zomboid"]


class _Stopping:
    def __init__(self, labels):
        self.id, self.name, self.labels = "c1", docker_ops.container_name(IID), labels
        self.status = "running"
        self.attrs = {"State": {"Running": True}}

    def reload(self):
        pass

    def stop(self, timeout=None):
        self.status = "exited"
        self.attrs = {"State": {"Running": False, "ExitCode": 0}}


@pytest.mark.parametrize(("game", "fixed"), [("satisfactory", True), (None, False), ("factorio", False)])
def test_a_stop_gives_the_files_the_runtime_owner(agent_settings, monkeypatch, game, fixed):
    calls: list = []
    monkeypatch.setattr(docker_ops, "fix_ownership", lambda s, iid, skip=(): calls.append(iid))
    monkeypatch.setattr(drivers, "for_container", lambda _c: drivers.NoControlDriver())
    labels = {docker_ops.LABEL_MANAGED: "true", docker_ops.LABEL_INSTANCE: str(IID)}
    if game:
        labels["io.mcpanel.game"] = game
    result = docker_ops.graceful_stop(agent_settings, _Stopping(labels), 30)
    assert result.exit_code == 0
    assert calls == ([IID] if fixed else [])


def test_a_backup_gives_the_files_the_runtime_owner_first(api, auth, agent_settings, folder, monkeypatch):
    order: list = []

    class FakeRestic:
        def __init__(self, *_a):
            pass

        def backup(self, src, tags, excludes, progress):
            order.append("restic")
            return {"snapshot_id": "abc"}

    monkeypatch.setattr(main, "Restic", FakeRestic)
    monkeypatch.setattr(docker_ops, "fix_ownership", lambda s, iid, skip=(): order.append(("fix", iid, skip)))
    for status, expected in (
        ({"exists": False, "last_run": {"game": "satisfactory"}}, [("fix", IID, ("gamefiles",)), "restic"]),
        ({"exists": False, "last_run": {}}, ["restic"]),  # Minecraft
    ):
        order.clear()
        monkeypatch.setattr(docker_ops, "status", lambda _iid, _s=None, st=status: st)
        r = api.post(f"/v1/instances/{IID}/backups", json={"backup_id": "b1"}, headers=auth)
        op_id = r.json()["operation"]["id"]
        for _ in range(300):
            op = api.get(f"/v1/operations/{op_id}", headers=auth).json()
            if op["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.01)
        assert op["status"] == "succeeded", op
        assert order == expected
