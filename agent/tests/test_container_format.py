"""The agent says how it creates containers (docker_ops.CONTAINER_FORMAT), and what a delete left behind.

The panel mixes the reported number into the hash behind "restart required", so a container
created by an agent that is older than the panel keeps reading restart-required until an
up-to-date agent has recreated it (backend ``apps.instances.spec``).
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from mcagent import docker_ops, main, storage

SPEC = {
    "name": "s",
    "image": "itzg/minecraft-server:java21",
    "memory_limit_mb": 2048,
    "rcon_password": "s3cret-rcon-password",
    "ports": [{"container_port": 25565, "host_port": 25565}],
}


def _wait(api, auth, response) -> dict:
    assert response.status_code == 200, response.text
    op_id = response.json()["operation"]["id"]
    deadline = time.monotonic() + 30
    while True:
        op = api.get(f"/v1/operations/{op_id}", headers=auth).json()
        if op["status"] not in ("pending", "running"):
            return op
        assert time.monotonic() < deadline, op
        time.sleep(0.02)


def test_container_format_matches_what_create_does(tmp_path):
    """Format 2 means "the container gets Docker's stop timeout": the number and the behaviour go together."""
    from mcagent.config import Settings
    from mcagent.schemas import InstanceSpec

    assert docker_ops.CONTAINER_FORMAT == 2
    settings = Settings(token="t" * 32, data_root=tmp_path)
    kwargs = docker_ops.build_create_kwargs(
        settings, uuid.uuid4(), InstanceSpec(**SPEC, stop_timeout=90), "hash"
    )
    assert kwargs["stop_timeout"] == min(90, settings.shutdown_stop_timeout)


def test_info_reports_the_container_format(api, auth, monkeypatch):
    monkeypatch.setattr(docker_ops, "docker_info", lambda: {"server_version": "27.5.1"})
    monkeypatch.setattr(storage, "capabilities", lambda _settings: {})
    monkeypatch.setattr(main, "restic_available", lambda _settings: None)
    r = api.get("/v1/info", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["container_format"] == docker_ops.CONTAINER_FORMAT


def test_apply_result_reports_the_container_format(api, auth, monkeypatch):
    iid = uuid.uuid4()
    created = SimpleNamespace(reload=lambda: None)
    calls: list[str] = []
    monkeypatch.setattr(docker_ops, "client", lambda: (_ for _ in ()).throw(AssertionError("real Docker")))
    monkeypatch.setattr(docker_ops, "local_image_id", lambda _image: "sha256:" + "ab" * 32)
    monkeypatch.setattr(docker_ops, "remove_stale_replacement", lambda _iid: False)
    monkeypatch.setattr(docker_ops, "get_container", lambda _iid: None)
    monkeypatch.setattr(
        docker_ops, "create_container", lambda *_a, **_k: (calls.append("create"), created)[1]
    )
    monkeypatch.setattr(docker_ops, "describe", lambda c: {"exists": True, "created_here": c is created})
    op = _wait(api, auth, api.put(f"/v1/instances/{iid}/container", json={"spec": SPEC}, headers=auth))
    assert op["status"] == "succeeded", op
    assert calls == ["create"]
    assert op["result"] == {
        "container": {"exists": True, "created_here": True},
        "recreated": False,
        "container_format": docker_ops.CONTAINER_FORMAT,
        "image_id_before": "sha256:" + "ab" * 32,  # what the tag pointed to (no container, no last run)
        "image_id": None,
        "pulled": False,
        "images_removed": [],
        "warnings": [],
    }


# -- DELETE /data: what stayed on disk is in the result, not only in a log --------------------------
def test_delete_data_reports_no_leftovers_when_everything_is_gone(api, auth, agent_settings, monkeypatch):
    monkeypatch.setattr(docker_ops, "remove_container", lambda *_a, **_k: False)
    iid = uuid.uuid4()
    inst = agent_settings.instances_dir / str(iid)
    (inst / "world").mkdir(parents=True)
    (inst / "world" / "level.dat").write_text("x")
    repo = agent_settings.backups_dir / "restic" / str(iid)
    repo.mkdir(parents=True)
    (repo / "config").write_text("x")
    op = _wait(
        api,
        auth,
        api.request("DELETE", f"/v1/instances/{iid}/data", json={"confirm": str(iid)}, headers=auth),
    )
    assert op["status"] == "succeeded", op
    assert op["result"] == {"deleted": True, "snapshots_deleted": 0, "warnings": [], "leftovers": []}
    assert not inst.exists() and not repo.exists() and list(agent_settings.trash_dir.iterdir()) == []


def test_delete_data_reports_snapshot_leftovers_and_files_it_could_not_remove(
    api, auth, agent_settings, monkeypatch
):
    """A snapshot that could not be deleted and a rest in the trash: both are named in the result."""
    from mcagent import snapshots

    monkeypatch.setattr(docker_ops, "remove_container", lambda *_a, **_k: False)
    iid = uuid.uuid4()
    inst = agent_settings.instances_dir / str(iid)
    inst.mkdir(parents=True)
    (inst / "server.properties").write_text("x")
    stuck = f"{agent_settings.data_root}/snapshots/{iid}/20261109T010203Z-stuck"

    class Provider:
        def destroy_all(self) -> dict:
            return {
                "destroyed": [],
                "snapshots": 0,
                "warnings": [f"Could not delete snapshot {stuck}: Operation not permitted."],
                "leftovers": [stuck],
            }

    monkeypatch.setattr(snapshots, "snapshot_provider", lambda _settings, _iid: Provider())
    real_rmtree = main.shutil.rmtree

    def rmtree(path, ignore_errors=False, **kwargs):
        if Path(path).parent == agent_settings.trash_dir:
            return  # e.g. a read-only nested subvolume: nothing could be removed
        real_rmtree(path, ignore_errors=ignore_errors, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(main.shutil, "rmtree", rmtree)
        op = _wait(
            api,
            auth,
            api.request("DELETE", f"/v1/instances/{iid}/data", json={"confirm": str(iid)}, headers=auth),
        )
    assert op["status"] == "succeeded", op  # the server is gone from the panel's point of view
    result = op["result"]
    [trash] = list(agent_settings.trash_dir.iterdir())
    assert result["deleted"] is True
    assert result["leftovers"] == [stuck, str(trash)]
    assert len(result["warnings"]) == 2
    assert stuck in result["warnings"][0]
    assert str(trash) in result["warnings"][1] and "server's files" in result["warnings"][1]
    assert not inst.exists()


def test_delete_data_works_with_a_provider_that_reports_no_leftovers(api, auth, agent_settings, monkeypatch):
    """ZfsSnapshots.destroy_all has no "leftovers" entry (it fails instead of leaving anything)."""
    from mcagent import snapshots

    monkeypatch.setattr(docker_ops, "remove_container", lambda *_a, **_k: False)
    iid = uuid.uuid4()
    provider = SimpleNamespace(destroy_all=lambda: {"destroyed": ["pool/x"], "snapshots": 3, "warnings": []})
    monkeypatch.setattr(snapshots, "snapshot_provider", lambda _settings, _iid: provider)
    op = _wait(
        api,
        auth,
        api.request("DELETE", f"/v1/instances/{iid}/data", json={"confirm": str(iid)}, headers=auth),
    )
    assert op["status"] == "succeeded", op
    assert op["result"] == {"deleted": True, "snapshots_deleted": 3, "warnings": [], "leftovers": []}
