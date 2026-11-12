"""B-7 of the security review (2026-11-11): a folder that holds a protected path cannot be deleted, renamed
away or replaced through the file API, and the compare folds case (a case-insensitive file system has one
entry for both spellings).

Adopted from the reviewers' tests (audit-2026-11-09/security-review/b-agent-fs/tests/test_b_file_api_guards.py;
its B-1 test of mounted sub-folders belongs to round 2 and is not here yet).
"""

from __future__ import annotations

import uuid

import pytest

from mcagent.claimmarker import MARKER, GuardedRoot, is_managed
from mcagent.profiles import cs2
from mcagent.safefs import PathError


def _guarded(path, protected):
    return GuardedRoot(path, None, None, settings=None, instance_id=uuid.uuid4(), protected=protected)


def _cs2_folder(tmp_path):
    cfg = tmp_path / "game" / "csgo" / "cfg"
    cfg.mkdir(parents=True)
    (cfg / "server.cfg").write_text("// generated\n")
    return cfg


def test_folder_that_holds_a_protected_file_cannot_be_deleted(tmp_path):
    cfg = _cs2_folder(tmp_path)
    root = _guarded(tmp_path, cs2.PROFILE.protected_paths)
    for rel in ("game/csgo/cfg", "game", "GAME/csgo"):
        with pytest.raises(PathError) as err:
            root.delete(rel, recursive=True)
        assert err.value.code == "managed_file"
    assert (cfg / "server.cfg").exists()


def test_folder_that_holds_a_protected_file_cannot_be_renamed_away_or_replaced(tmp_path):
    cfg = _cs2_folder(tmp_path)
    other = tmp_path / "upload"
    other.mkdir()
    (other / "server.cfg").write_text("// not generated\n")
    root = _guarded(tmp_path, cs2.PROFILE.protected_paths)
    with pytest.raises(PathError) as err:
        root.rename("game/csgo/cfg", "game/csgo/cfg.old")
    assert err.value.code == "managed_file"
    with pytest.raises(PathError):
        root.rename("upload", "game/csgo/cfg", overwrite=True)
    with pytest.raises(PathError):
        root.rename("upload/server.cfg", "game/csgo/cfg/SERVER.CFG", overwrite=True)
    assert (cfg / "server.cfg").read_text() == "// generated\n"


def test_a_file_cannot_take_the_place_of_a_folder_that_holds_one(tmp_path):
    root = _guarded(tmp_path, cs2.PROFILE.protected_paths)
    with pytest.raises(PathError) as err:
        root.open_writer("game/csgo")
    assert err.value.code == "managed_file"
    with pytest.raises(PathError):
        root.open_writer("Pre.sh")


def test_what_lies_beside_a_protected_path_stays_writable(tmp_path):
    cfg = _cs2_folder(tmp_path)
    root = _guarded(tmp_path, cs2.PROFILE.protected_paths)
    root.mkdir("game/csgo/maps", parents=True)  # a folder above a protected path may be made
    writer = root.open_writer("game/csgo/cfg/other.cfg")
    writer.write(b"x")
    writer.commit()
    root.rename("game/csgo/cfg/other.cfg", "game/csgo/cfg/other2.cfg")
    root.delete("game/csgo/cfg/other2.cfg")
    assert (cfg / "server.cfg").exists() and not (cfg / "other2.cfg").exists()


def test_the_claim_marker_is_matched_without_case():
    assert is_managed(MARKER) and is_managed(MARKER.upper()) and is_managed("./" + MARKER.title())
    assert not is_managed(MARKER + ".bak")


# --- B-1 / B-11 (round 2, interface I-2): mounted sub-folders, busy servers --------------------------------
@pytest.mark.parametrize(("game", "subdir"), [("valheim", "config"), ("project-zomboid", "data")])
def test_mounted_subfolder_cannot_be_replaced_through_the_file_api(game, subdir, tmp_path, monkeypatch):
    """Docker resolves a mount source again at every start; the file API must not swap a mounted
    sub-folder for a link between the start's check and Docker's mount (adopted from the review)."""
    import os

    from mcagent import docker_ops, main, profiles
    from mcagent.config import Settings
    from mcagent.schemas import InstanceSpec

    for var in list(os.environ):
        if var.startswith("AGENT_"):
            monkeypatch.delenv(var)
    settings = Settings(
        token="t" * 32,
        data_root=tmp_path / "data",
        restic_password="x",
        runtime_uid=os.getuid(),
        runtime_gid=os.getgid(),
    )
    instance = uuid.uuid4()
    folder = settings.instances_dir / str(instance)
    (folder / subdir).mkdir(parents=True)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    os.symlink(str(tmp_path), folder / subdir / "link")  # a link the game made inside its mount
    (folder / "elsewhere").mkdir()
    profile = profiles.get(game)
    spec = InstanceSpec(
        name="t", image=f"{profile.images[0]}:1", memory_limit_mb=1024, rcon_password="x" * 16, game=game
    )
    docker_ops.remember_server_game(settings, instance, spec)  # what every create does
    root = main._root(settings, instance)
    for attempt in (
        lambda: root.rename(subdir, f"{subdir}.old"),
        lambda: root.delete(subdir, recursive=True),
        lambda: root.rename("elsewhere", subdir, overwrite=True),
        lambda: root.rename("elsewhere", subdir.upper(), overwrite=True),
        lambda: root.open_writer(subdir),
        lambda: root.delete(".volumes", recursive=True),
    ):
        with pytest.raises(PathError) as err:
            attempt()
        assert err.value.code == "managed_file"
    assert (folder / subdir).is_dir() and not (folder / subdir).is_symlink()
    # its content stays the server's to edit
    writer = root.open_writer(f"{subdir}/notes.txt")
    writer.write(b"x")
    writer.commit()
    root.rename(f"{subdir}/notes.txt", f"{subdir}/notes2.txt")
    root.delete(f"{subdir}/notes2.txt")


def test_image_volume_folders_recorded_at_create_are_protected(tmp_path, monkeypatch):
    import os

    from mcagent import docker_ops, main, profiles
    from mcagent.config import Settings
    from mcagent.schemas import InstanceSpec

    for var in list(os.environ):
        if var.startswith("AGENT_"):
            monkeypatch.delenv(var)
    settings = Settings(
        token="t" * 32, data_root=tmp_path / "data", runtime_uid=os.getuid(), runtime_gid=os.getgid()
    )
    settings.state_dir.mkdir(parents=True)
    instance = uuid.uuid4()
    (settings.instances_dir / str(instance) / "saves").mkdir(parents=True)
    spec = InstanceSpec(
        name="f", image="factoriotools/factorio:2.0.55-rootless", memory_limit_mb=1024, rcon_password="x" * 16,
        game="factorio",
    )  # fmt: skip
    docker_ops.remember_server_game(settings, instance, spec, ["saves"])  # a VOLUME below the "." mount
    assert docker_ops.server_mount_points(settings, instance) == (".volumes", "saves")
    with pytest.raises(PathError):
        main._root(settings, instance).rename("saves", "saves.old")
    assert profiles.get("factorio").mounts  # (the profile itself mounts only ".")


def test_file_writes_wait_for_an_operation_that_replaces_the_folder(api, auth, agent_settings, monkeypatch):
    """B-11 (I-2): while a restore, an import, an apply ... runs, file writes answer 409 instance_busy."""
    import threading

    from mcagent import main

    iid = uuid.uuid4()
    (agent_settings.instances_dir / str(iid)).mkdir(parents=True)
    release = threading.Event()
    op = main.registry.start("restore", [iid], lambda _op: release.wait(10) and None)
    try:
        calls = [
            ("put", "/files/text", {"path": "a.txt", "content": "x"}),
            ("post", "/files/mkdir", {"path": "d"}),
            ("post", "/files/rename", {"source": "a", "destination": "b"}),
            ("post", "/files/delete", {"paths": ["a"]}),
            ("post", "/files/fetch", {"url": "https://example.com/a.jar", "path": "a.jar"}),
        ]
        for method, path, body in calls:
            r = getattr(api, method)(f"/v1/instances/{iid}{path}", json=body, headers=auth)
            assert r.status_code == 409 and r.json()["error"]["code"] == "instance_busy", (path, r.text)
        r = api.put(f"/v1/instances/{iid}/files/upload?path=u.bin", content=b"x", headers=auth)
        assert r.status_code == 409, r.text
        assert api.get(f"/v1/instances/{iid}/files/list", headers=auth).status_code == 200  # reads go on
    finally:
        release.set()
        for _ in range(100):
            if op.status not in ("pending", "running"):
                break
            threading.Event().wait(0.05)
    r = api.post(f"/v1/instances/{iid}/files/mkdir", json={"path": "d"}, headers=auth)
    assert r.status_code == 200, r.text


def test_a_mount_replaced_during_the_start_kills_the_container(agent_settings, monkeypatch):
    """B-1: the mount sources are checked again right after c.start(); a failure kills the container."""
    from types import SimpleNamespace

    from mcagent import docker_ops
    from mcagent.errors import AgentError

    events: list[str] = []
    c = SimpleNamespace(
        id="c1",
        name="mcpanel-x",
        labels={},
        start=lambda: events.append("start"),
        kill=lambda: events.append("kill"),
    )
    checks = iter([None, AgentError("config is no longer a real folder", "volume_source_invalid", 409)])

    def check(_s, _i, _c):
        result = next(checks)
        events.append("check")
        if result is not None:
            raise result

    monkeypatch.setattr(docker_ops, "require_container", lambda _iid: c)
    monkeypatch.setattr(docker_ops, "start_guard", lambda s, i, cc: docker_ops.check_bind_sources(s, i, cc))
    monkeypatch.setattr(docker_ops, "prepare_start", lambda *a: None)
    monkeypatch.setattr(docker_ops, "reattach_recreated_networks", lambda *a: [])
    monkeypatch.setattr(docker_ops, "check_bind_sources", check)
    with pytest.raises(AgentError) as err:
        docker_ops.start(uuid.uuid4(), agent_settings)
    assert err.value.code == "volume_source_invalid" and "stopped right after its start" in str(err.value)
    assert events == ["check", "start", "check", "kill"]
