"""World import/export: locating the world in an archive, the swap with rollback, and the HTTP API."""

from __future__ import annotations

import gzip
import io
import os
import time
import uuid
import zipfile

import pytest

from mcagent import world
from mcagent.docker_ops import AgentError
from mcagent.safefs import SafeRoot

LEVEL_DAT = gzip.compress(b"\x0a\x00\x00\x00")  # a gzip-compressed (empty) NBT compound


@pytest.fixture
def inst(tmp_path):
    d = tmp_path / "inst"
    d.mkdir()
    return d


@pytest.fixture
def sr(inst):
    return SafeRoot(inst)


def make_zip(path, entries: dict[str, bytes | str]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def old_world(inst, name="world", marker="old"):
    (inst / name / "region").mkdir(parents=True)
    (inst / name / "level.dat").write_bytes(LEVEL_DAT)
    (inst / name / "region" / "r.0.0.mca").write_text(marker)


def no_leftovers(inst) -> bool:
    return not [p.name for p in inst.iterdir() if p.name.startswith(world.STAGING_PREFIX)]


# --- level names ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["world", "My World", "worlds/survival", "Welt-ä"])
def test_valid_level_names(name):
    assert "/".join(world.level_parts(name)) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "  ",
        "../world",
        "a/../../b",
        ".hidden",
        "plugins",
        "mods",
        "Config",
        "a\\b",
        "x" * 201,
        "a/b/c/d/e",
    ],
)
def test_invalid_level_names(name):
    with pytest.raises(AgentError) as exc:
        world.level_parts(name)
    assert exc.value.code == "invalid_level_name"


def test_world_paths_per_layout():
    assert world.world_paths(["world"], "vanilla") == [("overworld", "world")]
    assert world.world_paths(["worlds", "s"], "bukkit") == [
        ("overworld", "worlds/s"),
        ("nether", "worlds/s_nether"),
        ("end", "worlds/s_the_end"),
    ]


# --- replace -------------------------------------------------------------------------------
def test_bare_world_archive_replaces_world(sr, inst):
    old_world(inst)
    (inst / "server.properties").write_text("level-name=world\n")
    make_zip(
        inst / "up.zip", {"level.dat": LEVEL_DAT, "region/r.0.0.mca": "new", "DIM-1/region/r.0.0.mca": "n"}
    )
    result = world.replace(sr, "up.zip", "world", "vanilla")
    assert (inst / "world" / "region" / "r.0.0.mca").read_text() == "new"
    assert (inst / "world" / "DIM-1" / "region" / "r.0.0.mca").read_text() == "n"
    assert (inst / "server.properties").read_text() == "level-name=world\n"
    assert result["installed"] == ["world"] and result["replaced"] == ["world"]
    assert result["archive_folder"] is None
    assert no_leftovers(inst)
    assert (inst / "up.zip").exists()  # the archive is the caller's business


def test_single_top_folder_is_stripped_and_renamed_to_level_name(sr, inst):
    old_world(inst, "survival")
    make_zip(inst / "up.zip", {"MyWorld/level.dat": LEVEL_DAT, "MyWorld/region/r.0.0.mca": "new"})
    result = world.replace(sr, "up.zip", "survival", "vanilla")
    assert (inst / "survival" / "region" / "r.0.0.mca").read_text() == "new"
    assert not (inst / "MyWorld").exists()
    assert result["archive_folder"] is None  # strip=auto removed the folder, so the root is the world


def test_whole_server_archive_only_imports_the_world(sr, inst):
    old_world(inst)
    (inst / "server.properties").write_text("motd=keep\n")
    make_zip(
        inst / "up.zip",
        {
            "server/server.properties": "motd=theirs\n",
            "server/plugins/x.jar": "jar",
            "server/world/level.dat": LEVEL_DAT,
            "server/world/region/r.0.0.mca": "new",
        },
    )
    result = world.replace(sr, "up.zip", "world", "vanilla")
    assert (inst / "world" / "region" / "r.0.0.mca").read_text() == "new"
    assert (inst / "server.properties").read_text() == "motd=keep\n"
    assert not (inst / "plugins").exists()
    assert any("plugins" in w and "server.properties" in w for w in result["warnings"])
    assert result["archive_folder"] == "world"


def test_nested_archive_is_searched(sr, inst):
    make_zip(
        inst / "up.zip", {"backup/2024/myworld/level.dat": LEVEL_DAT, "backup/2024/myworld/region/a.mca": "x"}
    )
    world.replace(sr, "up.zip", "world", "vanilla")
    assert (inst / "world" / "region" / "a.mca").read_text() == "x"


def test_bukkit_layout_replaces_all_dimension_folders(sr, inst):
    old_world(inst)
    old_world(inst, "world_nether", "old-nether")
    old_world(inst, "world_the_end", "old-end")
    make_zip(
        inst / "up.zip",
        {
            "world/level.dat": LEVEL_DAT,
            "world/region/r.0.0.mca": "new",
            "world_nether/level.dat": LEVEL_DAT,
            "world_nether/DIM-1/region/r.0.0.mca": "new-nether",
            "world_the_end/level.dat": LEVEL_DAT,
            "world_the_end/DIM1/region/r.0.0.mca": "new-end",
        },
    )
    result = world.replace(sr, "up.zip", "world", "bukkit")
    assert (inst / "world_nether" / "DIM-1" / "region" / "r.0.0.mca").read_text() == "new-nether"
    assert (inst / "world_the_end" / "DIM1" / "region" / "r.0.0.mca").read_text() == "new-end"
    assert not (inst / "world_nether" / "region").exists()  # the old nether is gone, not merged
    assert sorted(result["installed"]) == ["world", "world_nether", "world_the_end"]
    assert no_leftovers(inst)


def test_bukkit_server_with_vanilla_world_drops_the_old_dimensions(sr, inst):
    old_world(inst)
    old_world(inst, "world_nether", "old-nether")
    make_zip(inst / "up.zip", {"w/level.dat": LEVEL_DAT, "w/DIM-1/region/r.0.0.mca": "n"})
    result = world.replace(sr, "up.zip", "world", "bukkit")
    # Paper migrates world/DIM-1 into world_nether on the next start, which needs the old one gone
    assert not (inst / "world_nether").exists()
    assert (inst / "world" / "DIM-1" / "region" / "r.0.0.mca").read_text() == "n"
    assert result["replaced"] == ["world", "world_nether"]


def test_vanilla_server_with_bukkit_world_merges_dimensions(sr, inst):
    make_zip(
        inst / "up.zip",
        {
            "world/level.dat": LEVEL_DAT,
            "world_nether/DIM-1/region/a.mca": "n",
            "world_the_end/DIM1/region/a.mca": "e",
        },
    )
    result = world.replace(sr, "up.zip", "world", "vanilla")
    assert (inst / "world" / "DIM-1" / "region" / "a.mca").read_text() == "n"
    assert (inst / "world" / "DIM1" / "region" / "a.mca").read_text() == "e"
    assert not (inst / "world_nether").exists()
    assert sorted(result["converted"]) == ["end", "nether"]


def test_nested_level_name_creates_parent(sr, inst):
    make_zip(inst / "up.zip", {"level.dat": LEVEL_DAT})
    world.replace(sr, "up.zip", "worlds/survival", "vanilla")
    assert (inst / "worlds" / "survival" / "level.dat").read_bytes() == LEVEL_DAT


@pytest.mark.parametrize(
    ("entries", "code"),
    [
        ({"world/region/r.0.0.mca": "x", "readme.txt": "hi"}, "no_level_dat"),
        ({"a/level.dat": LEVEL_DAT, "b/level.dat": LEVEL_DAT}, "multiple_worlds"),
        ({"world/level.dat": b"not gzip"}, "invalid_world"),
        ({"world/level.dat": b"\x0a\x00\x00\x00", "world/db/CURRENT": "x"}, "bedrock_world"),
    ],
)
def test_invalid_archives_leave_the_world_untouched(sr, inst, entries, code):
    old_world(inst)
    make_zip(inst / "up.zip", entries)
    with pytest.raises(AgentError) as exc:
        world.replace(sr, "up.zip", "world", "vanilla")
    assert exc.value.code == code
    assert (inst / "world" / "region" / "r.0.0.mca").read_text() == "old"
    assert no_leftovers(inst)


def test_several_worlds_pick_the_level_name(sr, inst):
    make_zip(inst / "up.zip", {"a/level.dat": LEVEL_DAT, "survival/level.dat": LEVEL_DAT, "survival/x": "s"})
    world.replace(sr, "up.zip", "survival", "vanilla")
    assert (inst / "survival" / "x").read_text() == "s"


def test_not_enough_space(sr, inst):
    old_world(inst)
    make_zip(inst / "up.zip", {"world/level.dat": LEVEL_DAT})
    with pytest.raises(AgentError) as exc:
        world.replace(sr, "up.zip", "world", "vanilla", free_bytes=lambda: 1024)
    assert exc.value.code == "insufficient_space" and exc.value.status == 507
    assert (inst / "world" / "region" / "r.0.0.mca").read_text() == "old"
    assert no_leftovers(inst)


def test_failure_during_swap_restores_the_old_world(sr, inst, monkeypatch):
    old_world(inst)
    old_world(inst, "world_nether", "old-nether")
    make_zip(inst / "up.zip", {"world/level.dat": LEVEL_DAT, "world_nether/level.dat": LEVEL_DAT})
    real = SafeRoot.rename
    calls = []

    def flaky(self, src, dst, *, overwrite=False):
        calls.append((src, dst))
        if dst == "world_nether" and src.startswith(".mcpanel-world-new-"):
            raise OSError(28, "No space left on device")
        return real(self, src, dst, overwrite=overwrite)

    monkeypatch.setattr(SafeRoot, "rename", flaky)
    with pytest.raises(OSError):
        world.replace(sr, "up.zip", "world", "bukkit")
    assert (inst / "world" / "region" / "r.0.0.mca").read_text() == "old"
    assert (inst / "world_nether" / "region" / "r.0.0.mca").read_text() == "old-nether"
    assert no_leftovers(inst)


def test_rollback_failure_keeps_the_old_world_aside(sr, inst, monkeypatch):
    old_world(inst)
    make_zip(inst / "up.zip", {"world/level.dat": LEVEL_DAT, "world_nether/level.dat": LEVEL_DAT})
    real = SafeRoot.rename

    def broken(self, src, dst, *, overwrite=False):
        if dst == "world_nether" or (src.startswith(".mcpanel-world-old-") and dst == "world"):
            raise OSError(5, "I/O error")
        return real(self, src, dst, overwrite=overwrite)

    monkeypatch.setattr(SafeRoot, "rename", broken)
    with pytest.raises(AgentError) as exc:
        world.replace(sr, "up.zip", "world", "bukkit")
    assert exc.value.code == "world_rollback_failed"
    kept = [p for p in inst.iterdir() if p.name.startswith(".mcpanel-world-old-")]
    assert len(kept) == 1 and (kept[0] / "overworld" / "region" / "r.0.0.mca").read_text() == "old"
    assert kept[0].name in str(exc.value)


@pytest.mark.parametrize(
    ("archive", "level", "layout"),
    [
        ("world/w.zip", "world", "vanilla"),
        ("world/backups/w.zip", "world", "vanilla"),
        ("world_nether/w.zip", "world", "bukkit"),
        ("worlds/s_the_end/w.zip", "worlds/s", "bukkit"),
    ],
)
def test_archive_inside_a_replaced_world_folder_is_refused(sr, inst, archive, level, layout):
    old_world(inst, level)
    (inst / archive).parent.mkdir(parents=True, exist_ok=True)
    make_zip(inst / archive, {"level.dat": LEVEL_DAT, "region/r.0.0.mca": "new"})
    with pytest.raises(AgentError) as exc:
        world.replace(sr, archive, level, layout)
    assert exc.value.code == "archive_in_world"
    assert (inst / archive).exists()  # the archive would have been deleted with the old world
    assert (inst / level / "region" / "r.0.0.mca").read_text() == "old"
    assert no_leftovers(inst)


def test_archive_next_to_the_world_is_fine(sr, inst):
    old_world(inst)
    (inst / "world_backups").mkdir()  # same prefix, different folder
    make_zip(inst / "world_backups" / "w.zip", {"level.dat": LEVEL_DAT, "region/r.0.0.mca": "new"})
    world.replace(sr, "world_backups/w.zip", "world", "vanilla")
    assert (inst / "world" / "region" / "r.0.0.mca").read_text() == "new"
    assert (inst / "world_backups" / "w.zip").exists()


def test_a_file_where_the_world_folder_should_be_is_not_deleted(sr, inst):
    (inst / "world_nether").write_text("not a folder")
    make_zip(inst / "up.zip", {"level.dat": LEVEL_DAT})
    with pytest.raises(AgentError) as exc:
        world.replace(sr, "up.zip", "world", "bukkit")
    assert exc.value.code == "not_a_directory"
    assert (inst / "world_nether").read_text() == "not a folder"
    assert not (inst / "world").exists() and no_leftovers(inst)


def test_macos_resource_fork_folders_are_ignored(sr, inst):
    old_world(inst)
    # Finder zips add __MACOSX/ next to the content; it must not hide a nested world ...
    make_zip(
        inst / "nested.zip",
        {
            "Backups/MyWorld/level.dat": LEVEL_DAT,
            "Backups/MyWorld/region/r.0.0.mca": "new",
            "__MACOSX/Backups/MyWorld/._level.dat": "fork",
        },
    )
    result = world.replace(sr, "nested.zip", "world", "vanilla")
    assert (inst / "world" / "region" / "r.0.0.mca").read_text() == "new"
    assert not any("__MACOSX" in w for w in result["warnings"])
    # ... nor end up inside a world zipped from its contents
    make_zip(
        inst / "bare.zip",
        {"level.dat": LEVEL_DAT, "region/r.0.0.mca": "bare", "__MACOSX/._level.dat": "fork"},
    )
    world.replace(sr, "bare.zip", "world", "vanilla")
    assert (inst / "world" / "region" / "r.0.0.mca").read_text() == "bare"
    assert not (inst / "world" / "__MACOSX").exists()


def test_archive_must_be_a_file(sr, inst):
    (inst / "dir.zip").mkdir()
    with pytest.raises(AgentError) as exc:
        world.replace(sr, "dir.zip", "world", "vanilla")
    assert exc.value.code == "not_a_file"


# --- info --------------------------------------------------------------------------------------
def test_info_reports_folders_sizes_and_leftovers(sr, inst):
    old_world(inst)
    (inst / "world" / "DIM-1").mkdir()
    (inst / "world_nether").mkdir()
    (inst / ".mcpanel-world-old-abc").mkdir()
    data = world.info(sr, "world", "bukkit")
    roles = {f["role"]: f for f in data["folders"]}
    assert roles["overworld"]["exists"] and roles["overworld"]["has_level_dat"]
    assert roles["overworld"]["files"] == 2 and roles["overworld"]["bytes"] == len(LEVEL_DAT) + 3
    assert roles["overworld"]["dimensions"] == ["nether"]
    assert roles["nether"]["exists"] and not roles["nether"]["has_level_dat"]
    assert not roles["end"]["exists"]
    assert data["total_bytes"] == len(LEVEL_DAT) + 3
    assert data["leftovers"] == [".mcpanel-world-old-abc"]
    # what a reset would do with each folder
    assert roles["overworld"]["state"] == "world"
    assert roles["overworld"]["markers"] == ["DIM-1", "level.dat", "region"]
    assert roles["overworld"]["keepable"] == {}
    assert (roles["nether"]["state"], roles["nether"]["markers"], roles["nether"]["keepable"]) == (
        "empty",
        [],
        {},
    )
    assert (roles["end"]["state"], roles["end"]["markers"], roles["end"]["keepable"]) == ("missing", [], {})


def test_info_without_server_folder(tmp_path):
    data = world.info(SafeRoot(tmp_path / "missing"), "world", "vanilla")
    assert data["folders"][0]["exists"] is False and data["leftovers"] == []
    assert data["folders"][0]["state"] == "missing"


def test_info_without_sizes_reports_state_markers_and_keepable(sr, inst, monkeypatch, tmp_path):
    old_world(inst)
    (inst / "world" / "session.lock").write_text("")
    (inst / "world" / "datapacks").mkdir()
    (inst / "world" / "datapacks" / "terra.zip").write_text("dp")
    (inst / "world" / "serverconfig").mkdir()  # empty: nothing to carry over
    (inst / "world" / "paper-world.yml").write_text("a: 1")
    old_world(inst, "world_nether")
    (inst / "world_nether" / "paper-world.yml").write_text("b: 2")
    (inst / "world_nether" / "datapacks").mkdir()  # datapacks only count in the overworld folder
    (inst / "world_nether" / "datapacks" / "x.zip").write_text("dp")
    (inst / "world_the_end").mkdir()
    (inst / "world_the_end" / "paper-world.yml").write_text("c: 3")  # what an earlier reset carried over
    measured = world.info(sr, "world", "bukkit")

    def no_walk(self, rel="", *, max_depth=12):
        raise AssertionError("sizes=False must not walk the world")

    monkeypatch.setattr(SafeRoot, "walk_files", no_walk)
    data = world.info(sr, "world", "bukkit", sizes=False)
    roles = {f["role"]: f for f in data["folders"]}
    assert roles["overworld"]["state"] == "world"
    assert roles["overworld"]["markers"] == ["level.dat", "region", "session.lock"]
    assert roles["overworld"]["keepable"] == {"datapacks": ["datapacks"], "config": ["paper-world.yml"]}
    assert roles["nether"]["state"] == "world" and roles["nether"]["markers"] == ["level.dat", "region"]
    assert roles["nether"]["keepable"] == {"config": ["paper-world.yml"]}
    assert (roles["end"]["state"], roles["end"]["markers"], roles["end"]["keepable"]) == ("pending", [], {})
    for folder in data["folders"]:
        assert (folder["bytes"], folder["files"], folder["modified"]) == (0, 0, None)
        assert folder["exists"] is True and folder["type"] == "directory"
    assert data["total_bytes"] == 0 and roles["overworld"]["has_level_dat"] is True
    # apart from the measurements the answer is the same
    unmeasured = {"bytes", "files", "modified"}
    assert [{k: v for k, v in f.items() if k not in unmeasured} for f in data["folders"]] == [
        {k: v for k, v in f.items() if k not in unmeasured} for f in measured["folders"]
    ]
    assert measured["folders"][0]["files"] == 5 and measured["total_bytes"] > 0


def test_info_states_of_folders_that_are_not_worlds(sr, inst, tmp_path):
    (inst / "world").write_text("a file")
    (inst / "world_nether" / "web").mkdir(parents=True)  # some other folder
    (inst / "world_nether" / "web" / "index.html").write_text("x")
    os.symlink(tmp_path, inst / "world_the_end")
    for sizes in (True, False):
        roles = {f["role"]: f for f in world.info(sr, "world", "bukkit", sizes=sizes)["folders"]}
        assert (roles["overworld"]["state"], roles["overworld"]["type"]) == ("not_a_directory", "file")
        assert (roles["nether"]["state"], roles["nether"]["exists"]) == ("unknown", True)
        assert (roles["end"]["state"], roles["end"]["type"]) == ("not_a_directory", "symlink")
        assert all(f["markers"] == [] and f["keepable"] == {} for f in roles.values())
        assert not roles["overworld"]["exists"] and not roles["end"]["exists"]


# --- HTTP API ------------------------------------------------------------------------------------
def _wait(api, auth, op_id, timeout=10):
    deadline = time.monotonic() + timeout
    while True:
        op = api.get(f"/v1/operations/{op_id}", headers=auth).json()
        if op["status"] in ("succeeded", "failed"):
            return op
        assert time.monotonic() < deadline
        time.sleep(0.05)


@pytest.fixture
def stopped(monkeypatch):
    from mcagent import docker_ops

    state = {"running": False}
    monkeypatch.setattr(docker_ops, "status", lambda _iid: {"exists": True, "running": state["running"]})
    return state


def _instance(agent_settings):
    iid = str(uuid.uuid4())
    d = agent_settings.instances_dir / iid
    d.mkdir(parents=True)
    return iid, d


def test_api_world_replace_and_info(api, auth, agent_settings, stopped):
    iid, d = _instance(agent_settings)
    old_world(d)
    make_zip(d / "up.zip", {"world/level.dat": LEVEL_DAT, "world/region/r.0.0.mca": "new"})
    info = api.get(f"/v1/instances/{iid}/world", params={"level_name": "world"}, headers=auth).json()
    assert info["folders"][0]["has_level_dat"] is True

    stopped["running"] = True
    resp = api.post(f"/v1/instances/{iid}/world/replace", json={"archive": "up.zip"}, headers=auth)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "must_stop"
    stopped["running"] = False

    bad = api.post(
        f"/v1/instances/{iid}/world/replace", json={"archive": "up.zip", "level_name": "../x"}, headers=auth
    )
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_level_name"

    resp = api.post(
        f"/v1/instances/{iid}/world/replace", json={"archive": "up.zip", "delete_archive": True}, headers=auth
    )
    assert resp.status_code == 200
    op = _wait(api, auth, resp.json()["operation"]["id"])
    assert op["status"] == "succeeded", op
    assert op["result"]["installed"] == ["world"]
    assert (d / "world" / "region" / "r.0.0.mca").read_text() == "new"
    assert not (d / "up.zip").exists()


def test_api_world_info_sizes_parameter(api, auth, agent_settings):
    iid, d = _instance(agent_settings)
    old_world(d)
    url = f"/v1/instances/{iid}/world"
    full = api.get(url, params={"level_name": "world"}, headers=auth).json()["folders"][0]
    assert (full["files"], full["bytes"]) == (2, len(LEVEL_DAT) + 3) and full["modified"] is not None
    quick = api.get(url, params={"level_name": "world", "sizes": "false"}, headers=auth).json()
    assert quick["total_bytes"] == 0
    assert (quick["folders"][0]["files"], quick["folders"][0]["bytes"], quick["folders"][0]["modified"]) == (
        0,
        0,
        None,
    )
    for folder in (full, quick["folders"][0]):
        assert (folder["state"], folder["markers"], folder["keepable"]) == (
            "world",
            ["level.dat", "region"],
            {},
        )
    assert api.get(url, params={"sizes": "maybe"}, headers=auth).status_code == 422


def test_api_refuses_an_archive_inside_the_world(api, auth, agent_settings, stopped):
    iid, d = _instance(agent_settings)
    old_world(d)
    make_zip(d / "world" / "w.zip", {"level.dat": LEVEL_DAT})
    resp = api.post(f"/v1/instances/{iid}/world/replace", json={"archive": "world/w.zip"}, headers=auth)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "archive_in_world"


def test_api_server_started_before_the_operation_ran(api, auth, agent_settings, monkeypatch):
    """A start that slips in between the endpoint's check and the operation must not see a swap."""
    from mcagent import docker_ops

    iid, d = _instance(agent_settings)
    old_world(d)
    make_zip(d / "up.zip", {"world/level.dat": LEVEL_DAT, "world/region/r.0.0.mca": "new"})
    answers = iter([{"exists": True, "running": False}, {"exists": True, "running": True}])
    monkeypatch.setattr(docker_ops, "status", lambda _iid: next(answers))
    resp = api.post(
        f"/v1/instances/{iid}/world/replace", json={"archive": "up.zip", "delete_archive": True}, headers=auth
    )
    op = _wait(api, auth, resp.json()["operation"]["id"])
    assert op["status"] == "failed" and op["error_code"] == "must_stop"
    assert (d / "world" / "region" / "r.0.0.mca").read_text() == "old"
    assert not (d / "up.zip").exists() and no_leftovers(d)


def test_api_failed_replace_still_deletes_the_temporary_archive(api, auth, agent_settings, stopped):
    iid, d = _instance(agent_settings)
    old_world(d)
    make_zip(d / "up.zip", {"readme.txt": "no world here"})
    resp = api.post(
        f"/v1/instances/{iid}/world/replace", json={"archive": "up.zip", "delete_archive": True}, headers=auth
    )
    op = _wait(api, auth, resp.json()["operation"]["id"])
    assert op["status"] == "failed" and op["error_code"] == "no_level_dat"
    assert not (d / "up.zip").exists()
    assert (d / "world" / "region" / "r.0.0.mca").read_text() == "old"


def test_api_zip_of_several_folders(api, auth, agent_settings):
    iid, d = _instance(agent_settings)
    old_world(d)
    old_world(d, "world_nether", "nether")
    (d / "server.properties").write_text("x")
    resp = api.get(f"/v1/instances/{iid}/files/archive?path=world&path=world_nether", headers=auth)
    assert resp.status_code == 200
    names = set(zipfile.ZipFile(io.BytesIO(resp.content)).namelist())
    assert names == {
        "world/level.dat",
        "world/region/r.0.0.mca",
        "world_nether/level.dat",
        "world_nether/region/r.0.0.mca",
    }
    single = api.get(f"/v1/instances/{iid}/files/archive?path=world", headers=auth)
    assert set(zipfile.ZipFile(io.BytesIO(single.content)).namelist()) == {
        "world/level.dat",
        "world/region/r.0.0.mca",
    }
    (d / "other").mkdir()
    (d / "other" / "world").mkdir()
    dup = api.get(f"/v1/instances/{iid}/files/archive?path=world&path=other/world", headers=auth)
    assert dup.status_code == 400
    missing = api.get(f"/v1/instances/{iid}/files/archive?path=world&path=nope", headers=auth)
    assert missing.status_code == 404
    too_many = api.get(f"/v1/instances/{iid}/files/archive?" + "&".join(["path=world"] * 9), headers=auth)
    assert too_many.status_code == 400
