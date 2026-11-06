"""Real btrfs snapshot tests.

Run against a btrfs mount given by MCPANEL_TEST_BTRFS (e.g. a loopback image):
    truncate -s 2G /srv/btrfs.img && mkfs.btrfs /srv/btrfs.img && mount -o loop /srv/btrfs.img /srv/btrfs-test
    MCPANEL_TEST_BTRFS=/srv/btrfs-test uv run pytest tests/test_snapshots_btrfs.py
"""

import os
import shutil
import uuid
from pathlib import Path

import pytest

from mcagent.config import Settings
from mcagent.docker_ops import AgentError
from mcagent.snapshots import BtrfsSnapshots, ensure_instance_dir, is_subvolume

ROOT = os.environ.get("MCPANEL_TEST_BTRFS")
pytestmark = pytest.mark.skipif(
    not ROOT or not shutil.which("btrfs"), reason="needs MCPANEL_TEST_BTRFS btrfs mount"
)


@pytest.fixture
def setup():
    base = Path(ROOT) / f"t-{uuid.uuid4().hex[:8]}"
    settings = Settings(data_root=base)
    settings.trash_dir.mkdir(parents=True, exist_ok=True)
    iid = uuid.uuid4()
    yield settings, iid
    for p in sorted(base.rglob("*"), key=lambda x: -len(str(x))):
        if p.is_dir() and is_subvolume(p):
            os.system(f"btrfs -q subvolume delete '{p}' >/dev/null 2>&1")  # test cleanup only
    shutil.rmtree(base, ignore_errors=True)


def test_new_instance_dir_is_subvolume_and_snapshot_restore(setup):
    settings, iid = setup
    inst = settings.instances_dir / str(iid)
    assert ensure_instance_dir(settings, inst) is True
    assert is_subvolume(inst)
    (inst / "world").mkdir()
    (inst / "world" / "level.dat").write_text("v1")
    snaps = BtrfsSnapshots(settings, iid)
    assert snaps.capability()["available"] and snaps.capability()["instance_is_subvolume"]
    s1 = snaps.create("Before upgrade!")
    assert s1["name"].endswith("-before-upgrade")
    (inst / "world" / "level.dat").write_text("v2-broken")
    (inst / "new.txt").write_text("added later")
    assert [s["name"] for s in snaps.list()] == [s1["name"]]
    snaps.restore(s1["name"])
    assert (inst / "world" / "level.dat").read_text() == "v1"
    assert not (inst / "new.txt").exists()
    assert is_subvolume(inst)  # still a subvolume after rollback
    # restored instance is writable and can be snapshotted again
    (inst / "after.txt").write_text("ok")
    snaps.delete(s1["name"])
    assert snaps.list() == []
    assert not list(inst.parent.glob("*.mcpanel-old-*"))


def test_convert_plain_directory(setup):
    settings, iid = setup
    inst = settings.instances_dir / str(iid)
    inst.mkdir(parents=True)
    (inst / "server.properties").write_text("motd=x\n")
    os.symlink("/etc/passwd", inst / "link")  # symlinks are copied as links, never followed
    snaps = BtrfsSnapshots(settings, iid)
    assert snaps.capability()["instance_is_subvolume"] is False
    with pytest.raises(AgentError):
        snaps.create("x")
    assert snaps.convert() == {"converted": True}
    assert is_subvolume(inst)
    assert (inst / "server.properties").read_text() == "motd=x\n"
    assert os.path.islink(inst / "link")
    assert snaps.create()["name"]


def test_purge_deletes_read_only_snapshots(setup):
    settings, iid = setup
    inst = settings.instances_dir / str(iid)
    ensure_instance_dir(settings, inst)
    (inst / "level.dat").write_text("v1")
    snaps = BtrfsSnapshots(settings, iid)
    names = [snaps.create("one")["name"], snaps.create("two")["name"]]
    assert all(is_subvolume(snaps.snap_dir / n) for n in names)
    assert snaps.destroy_all()["snapshots"] == 2  # purge() became the provider-wide destroy_all()
    assert not snaps.snap_dir.exists()
    assert snaps.destroy_all()["snapshots"] == 0  # nothing left: a no-op


def test_delete_data_removes_snapshots(setup, monkeypatch):
    """Deleting a server must not orphan its snapshots (read-only subvolumes outside its folder)."""
    import time

    from fastapi.testclient import TestClient

    from mcagent import docker_ops
    from mcagent.config import get_settings
    from mcagent.main import app

    settings, iid = setup
    settings = settings.model_copy(update={"token": "t" * 32})
    inst = settings.instances_dir / str(iid)
    ensure_instance_dir(settings, inst)
    (inst / "level.dat").write_text("v1")
    snaps = BtrfsSnapshots(settings, iid)
    snaps.create("kept")
    monkeypatch.setattr(docker_ops, "remove_container", lambda *_a, **_k: False)
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        api = TestClient(app)
        headers = {"Authorization": f"Bearer {'t' * 32}"}
        op = api.request(
            "DELETE", f"/v1/instances/{iid}/data", json={"confirm": str(iid)}, headers=headers
        ).json()
        for _ in range(100):
            state = api.get(f"/v1/operations/{op['operation']['id']}", headers=headers).json()
            if state["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.1)
    finally:
        app.dependency_overrides.pop(get_settings, None)
    assert state["status"] == "succeeded", state
    assert not inst.exists()
    assert not snaps.snap_dir.exists()


def test_invalid_names_rejected(setup):
    settings, iid = setup
    ensure_instance_dir(settings, settings.instances_dir / str(iid))
    snaps = BtrfsSnapshots(settings, iid)
    for bad in ["../x", "20260101T000000Z/../../etc", "nope"]:
        with pytest.raises(AgentError):
            snaps.delete(bad)


def test_destroy_all_and_in_place_swap(setup):
    from mcagent.snapshots import is_volume_root, swap_contents

    settings, iid = setup
    inst = settings.instances_dir / str(iid)
    ensure_instance_dir(settings, inst)
    (inst / "a.txt").write_text("old")
    snaps = BtrfsSnapshots(settings, iid)
    snaps.create("one")
    assert is_volume_root(inst) and not is_volume_root(inst.parent)
    staging = inst / ".mcpanel-restore-1"
    staging.mkdir()
    (staging / "b.txt").write_text("new")
    swap_contents(inst, staging)  # renames stay inside the subvolume (cross-subvolume would be EXDEV)
    assert sorted(p.name for p in inst.iterdir()) == ["b.txt"] and is_subvolume(inst)
    result = snaps.destroy_all()
    assert result["snapshots"] == 1 and result["warnings"] == []
    assert not snaps.snap_dir.exists()


def _wait(op_id: str, seconds: float = 60):
    import time

    from mcagent.main import registry

    op = registry.get(op_id)
    deadline = time.monotonic() + seconds
    while op.status in ("pending", "running"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return op


@pytest.mark.skipif(shutil.which("restic") is None, reason="restic binary not installed")
def test_backup_restore_path_into_a_subvolume(setup, monkeypatch):
    """Single-path restores must not rename across the subvolume boundary (EXDEV)."""
    from fastapi.testclient import TestClient

    from mcagent import docker_ops
    from mcagent.backups import Restic
    from mcagent.config import get_settings
    from mcagent.main import app
    from tests.conftest import TOKEN

    base, iid = setup
    settings = Settings(
        token=TOKEN,
        data_root=base.data_root,
        restic_password="test-restic-password",
        runtime_uid=os.getuid(),
        runtime_gid=os.getgid(),
    )
    for d in (settings.backups_dir, settings.staging_dir):
        d.mkdir(parents=True, exist_ok=True)
    inst = settings.instances_dir / str(iid)
    assert ensure_instance_dir(settings, inst) and is_subvolume(inst)
    (inst / "world").mkdir()
    (inst / "world" / "level.dat").write_text("ORIGINAL")
    (inst / "server.properties").write_text("motd=x\n")
    snap = Restic(settings, iid).backup(inst, [f"instance:{iid}"], [])["snapshot_id"]
    (inst / "world" / "level.dat").write_text("BROKEN")
    monkeypatch.setattr(docker_ops, "status", lambda i: {"running": False})
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        r = TestClient(app).post(
            f"/v1/instances/{iid}/backups/{snap}/restore-path",
            json={"path": "world"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert r.status_code == 200, r.text
        op = _wait(r.json()["operation"]["id"])
    finally:
        app.dependency_overrides.pop(get_settings, None)
    assert op.status == "succeeded", op.as_dict()
    assert (inst / "world" / "level.dat").read_text() == "ORIGINAL"
    assert sorted(p.name for p in inst.iterdir()) == ["server.properties", "world"]  # no staging leftovers
    assert is_subvolume(inst)
