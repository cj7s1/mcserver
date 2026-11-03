"""storage: /proc/self/mountinfo parsing, snapshot capability reasons, usage accounting."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from mcagent import storage

MOUNTINFO = r"""22 1 0:21 / / rw,relatime shared:1 - overlay overlay rw,lowerdir=/x,upperdir=/y,workdir=/z
30 22 0:25 / /proc rw,nosuid,nodev,noexec,relatime shared:2 - proc proc rw
35 22 0:31 / /mnt/user rw,noatime shared:5 - fuse.shfs shfs rw,user_id=0,group_id=0
36 22 8:17 /appdata /mnt/cache rw,noatime shared:6 - btrfs /dev/sdb1 rw,ssd,space_cache=v2
37 22 8:33 / /mnt/cachey rw,noatime - ext4 /dev/sdc1 rw
38 22 8:49 / /mnt/my\040disk rw,noatime - xfs /dev/sdd1 rw
39 22 0:50 / /mnt/pool rw - zfs tank/pool rw,xattr
40 39 0:51 / /mnt/pool rw - zfs tank/pool-override rw
41 22 0:52 / /srv/data rw - ext4 /dev/sde1 rw
malformed line without separator
"""


@pytest.fixture
def mountinfo(monkeypatch):
    real_path = storage.Path

    class FakePath(type(Path())):
        def read_text(self, *args, **kwargs):
            if str(self) == "/proc/self/mountinfo":
                return MOUNTINFO
            return super().read_text(*args, **kwargs)

    monkeypatch.setattr(storage, "Path", FakePath)
    yield
    monkeypatch.setattr(storage, "Path", real_path)


@pytest.mark.parametrize(
    ("path", "mount_point", "fstype", "source"),
    [
        ("/mnt/cache/appdata/mcpanel", "/mnt/cache", "btrfs", "/dev/sdb1"),
        ("/mnt/cache", "/mnt/cache", "btrfs", "/dev/sdb1"),
        ("/mnt/cachey/mc", "/mnt/cachey", "ext4", "/dev/sdc1"),  # not confused with /mnt/cache
        ("/mnt/user/appdata/mcpanel", "/mnt/user", "fuse.shfs", "shfs"),
        ("/mnt/my disk/servers", "/mnt/my disk", "xfs", "/dev/sdd1"),  # octal-escaped space
        ("/mnt/pool/mc", "/mnt/pool", "zfs", "tank/pool-override"),  # stacked mount: last wins
        ("/srv/data/instances", "/srv/data", "ext4", "/dev/sde1"),
        ("/opt/other", "/", "overlay", "overlay"),
    ],
)
def test_mount_for(mountinfo, path, mount_point, fstype, source):
    m = storage.mount_for(Path(path))
    assert (m["mount_point"], m["fstype"], m["source"]) == (mount_point, fstype, source)


def test_mount_root_field(mountinfo):
    assert storage.mount_for(Path("/mnt/cache/x"))["root"] == "/appdata"


def test_mount_for_real_system_returns_something(tmp_path):
    m = storage.mount_for(tmp_path)
    assert m is not None and m["mount_point"] and m["fstype"]


@pytest.mark.parametrize(
    ("fstype", "provider", "reason_part"),
    [
        ("fuse.shfs", None, "Unraid user shares"),
        ("ext4", None, "no native snapshot support"),
        ("btrfs", "btrfs", None),
        ("zfs", "zfs", None),
    ],
)
def test_describe_path_snapshot_capability(tmp_path, monkeypatch, fstype, provider, reason_part):
    monkeypatch.setattr(
        storage, "mount_for", lambda p: {"mount_point": "/x", "fstype": fstype, "source": "s", "root": "/"}
    )
    monkeypatch.setattr(storage.shutil, "which", lambda name: None)  # no btrfs/zfs tools in the image
    info = storage.describe_path(tmp_path / "instances")
    assert (tmp_path / "instances").is_dir()
    assert info["fstype"] == fstype
    assert info["snapshot"]["provider"] == provider
    assert info["snapshot"]["available"] is False
    assert info["total_bytes"] > 0 and info["free_bytes"] >= 0
    if reason_part:
        assert reason_part in info["snapshot"]["reason"]
    else:
        assert "not installed" in info["snapshot"]["reason"]


def test_dir_size_counts_files_and_skips_symlink_targets(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f").write_bytes(b"x" * 10000)
    big_outside = tmp_path.parent / f"outside-{uuid.uuid4().hex}"
    big_outside.write_bytes(b"y" * 1_000_000)
    try:
        (tmp_path / "link").symlink_to(big_outside)
        size = storage.dir_size(tmp_path)
        assert 10000 <= size < 1_000_000
    finally:
        big_outside.unlink()
    assert storage.dir_size(tmp_path / "missing") == 0


def test_instance_usage_is_cached(agent_settings):
    iid = uuid.uuid4()
    d = agent_settings.instances_dir / str(iid)
    d.mkdir()
    (d / "f").write_bytes(b"x" * 5000)
    first = storage.instance_usage(agent_settings, iid)
    assert first["cached"] is False and first["bytes"] >= 5000
    (d / "g").write_bytes(b"x" * 50000)
    assert storage.instance_usage(agent_settings, iid)["cached"] is True
    fresh = storage.instance_usage(agent_settings, iid, max_age=0)
    assert fresh["cached"] is False and fresh["bytes"] >= 55000
