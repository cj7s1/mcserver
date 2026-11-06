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
