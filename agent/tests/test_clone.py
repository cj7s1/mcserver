"""Clone: the copy belongs to the runtime user (audit agent-files#1) and "leave out worlds" leaves
out exactly the world of the server's level-name (agent-files#8). Real temporary folders; the
owner change is recorded instead of done (the tests do not run as root)."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest

from mcagent import docker_ops


def _wait(api, auth, r) -> dict:
    assert r.status_code == 200, r.text
    op_id = r.json()["operation"]["id"]
    deadline = time.monotonic() + 30
    while (op := api.get(f"/v1/operations/{op_id}", headers=auth).json())["status"] in ("pending", "running"):
        assert time.monotonic() < deadline, op
        time.sleep(0.02)
    return op


def _tree(root: Path) -> set[str]:
    out = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            out.add(os.path.relpath(os.path.join(dirpath, name), root))
    return out


def _put(root: Path, *files: str) -> None:
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rel)


@pytest.fixture
def source(agent_settings, monkeypatch):
    monkeypatch.setattr(docker_ops, "status", lambda _iid: {"running": False})
    iid = uuid.uuid4()
    root = agent_settings.instances_dir / str(iid)
    _put(
        root,
        "server.properties",
        "bruh/level.dat",
        "bruh/region/r.0.0.mca",
        "bruh_nether/DIM-1/region/r.0.0.mca",
        "bruh_the_end/DIM1/region/r.0.0.mca",
        "world/level.dat",  # not the level of this server (level-name=bruh): an older world
        "plugins/WorldGuard/worlds/world/regions.yml",
        "plugins/WorldGuard/worlds/bruh/regions.yml",
        "worlds/main/level.dat",
        "worlds/main_nether/level.dat",
        "worlds/other/level.dat",
    )
    os.symlink("server.properties", root / "link")
    return iid, root


def _clone(api, auth, agent_settings, src, **body):
    dst = uuid.uuid4()
    op = _wait(
        api,
        auth,
        api.post(f"/v1/instances/{src}/clone", json={"target_instance_id": str(dst), **body}, headers=auth),
    )
    return op, agent_settings.instances_dir / str(dst)


# -- agent-files#8 --------------------------------------------------------------------------------
def test_leave_out_worlds_skips_exactly_the_bukkit_world_of_the_level_name(api, auth, agent_settings, source):
    src, root = source
    op, out = _clone(api, auth, agent_settings, src, exclude_worlds=True, level_name="bruh", layout="bukkit")
    assert op["status"] == "succeeded", op
    # the world of the level-name, and the older world/ (it holds a level.dat itself)
    assert op["result"]["skipped_worlds"] == ["bruh", "bruh_nether", "bruh_the_end", "world"]
    left_out = ("bruh", "bruh_nether", "bruh_the_end", "world")
    assert _tree(out) == {p for p in _tree(root) if p.split("/")[0] not in left_out}
    # folders called like a world deeper down are plugin data, not the world
    assert (out / "plugins/WorldGuard/worlds/bruh/regions.yml").exists()
    assert (out / "plugins/WorldGuard/worlds/world/regions.yml").exists()


def test_leave_out_worlds_vanilla_layout_only_the_level_folder_at_the_top(api, auth, agent_settings, source):
    src, root = source
    op, out = _clone(
        api, auth, agent_settings, src, exclude_worlds=True, level_name="world", layout="vanilla"
    )
    assert op["status"] == "succeeded", op
    assert op["result"]["skipped_worlds"] == ["bruh", "world"]  # bruh/ holds a level.dat itself
    assert _tree(out) == {p for p in _tree(root) if p.split("/")[0] not in ("bruh", "world")}
    assert (out / "plugins/WorldGuard/worlds/world/regions.yml").exists()
    assert (out / "bruh_nether/DIM-1/region/r.0.0.mca").exists()


def test_leave_out_worlds_with_a_level_name_in_a_subfolder(api, auth, agent_settings, source):
    src, _root = source
    op, out = _clone(
        api, auth, agent_settings, src, exclude_worlds=True, level_name="worlds/main", layout="bukkit"
    )
    assert op["status"] == "succeeded", op
    assert op["result"]["skipped_worlds"] == ["bruh", "world", "worlds/main", "worlds/main_nether"]
    # only folders at the top count as worlds by their level.dat: worlds/other is copied
    assert (out / "worlds/other/level.dat").exists() and not (out / "worlds/main").exists()


def test_without_a_level_name_the_old_behaviour_stays(api, auth, agent_settings, source):
    src, _root = source
    op, out = _clone(api, auth, agent_settings, src, exclude_worlds=True)
    assert op["status"] == "succeeded", op
    assert not (out / "world").exists() and not (out / "plugins/WorldGuard/worlds/world").exists()
    assert not (out / "bruh").exists()  # holds a level.dat
    assert (out / "bruh_nether/DIM-1/region/r.0.0.mca").exists()  # no level.dat of its own


@pytest.mark.parametrize("level_name", ["bruh", None])
def test_leave_out_worlds_also_leaves_out_other_worlds_and_the_agents_world_folders(
    api, auth, agent_settings, source, level_name
):
    """review r3 #26 (real-stack test): an older world/ and .mcpanel-world-old-* were copied."""
    src, root = source
    _put(
        root,
        ".mcpanel-world-old-1234/level.dat",  # the world a failed reset moved aside
        ".mcpanel-world-old-1234/region/r.0.0.mca",
        ".mcpanel-world-new-abcd/x",  # an import that was being extracted
        "old-survival/level.dat",
        "backups/w/level.dat",  # a level.dat further down: not a world folder at the top
        "notes/level.dat.txt",
    )
    (root / "odd").mkdir()
    (root / "odd" / "level.dat").mkdir()  # not a file
    os.symlink("old-survival", root / "linked-world")  # copied as the link it is, never followed
    body = {"exclude_worlds": True}
    if level_name:
        body |= {"level_name": level_name, "layout": "bukkit"}
    op, out = _clone(api, auth, agent_settings, src, **body)
    assert op["status"] == "succeeded", op
    skipped = op["result"]["skipped_worlds"]
    for name in (".mcpanel-world-new-abcd", ".mcpanel-world-old-1234", "old-survival", "world", "bruh"):
        assert name in skipped and not os.path.lexists(out / name)
    assert (out / "backups/w/level.dat").exists() and (out / "notes/level.dat.txt").exists()
    assert (out / "odd/level.dat").is_dir() and "odd" not in skipped
    assert os.readlink(out / "linked-world") == "old-survival" and "linked-world" not in skipped


def test_without_exclude_worlds_the_level_name_changes_nothing(api, auth, agent_settings, source):
    src, root = source
    op, out = _clone(api, auth, agent_settings, src, level_name="bruh", layout="bukkit")
    assert op["status"] == "succeeded", op
    assert _tree(out) == _tree(root) and "skipped_worlds" not in op["result"]


@pytest.mark.parametrize("level", ["../escape", "plugins", ".hidden", "a/b/c/d/e"])
def test_a_bad_level_name_is_refused_before_anything_is_copied(api, auth, agent_settings, source, level):
    src, _root = source
    dst = uuid.uuid4()
    r = api.post(
        f"/v1/instances/{src}/clone",
        json={"target_instance_id": str(dst), "exclude_worlds": True, "level_name": level},
        headers=auth,
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_level_name", r.text
    assert not (agent_settings.instances_dir / str(dst)).exists()


# -- agent-files#1 --------------------------------------------------------------------------------
def test_every_copied_entry_is_given_to_the_runtime_user(api, auth, agent_settings, source, monkeypatch):
    src, root = source
    agent_settings.runtime_uid, agent_settings.runtime_gid = 99, 100
    chowned: dict[str, tuple[int, int, bool]] = {}
    real_chown = os.chown

    def chown(path, uid, gid, *, dir_fd=None, follow_symlinks=True):
        if uid == 99:
            where = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), path) if dir_fd is not None else path
            chowned[os.path.realpath(os.path.dirname(where)) + "/" + os.path.basename(where)] = (
                uid,
                gid,
                follow_symlinks,
            )
            return None
        return real_chown(path, uid, gid, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "chown", chown)
    op, out = _clone(api, auth, agent_settings, src)
    assert op["status"] == "succeeded", op
    # the copy is made in a staging folder and renamed into place: its top folder comes first
    top = next(iter(chowned))
    assert Path(top).name == f"clone-{out.name}"
    copied = {os.path.relpath(p, top) for p in chowned if p != top}
    assert copied == _tree(root)  # every file, folder and link below the top folder
    assert all(v == (99, 100, False) for v in chowned.values())  # links themselves, never followed
    assert os.path.islink(out / "link")
