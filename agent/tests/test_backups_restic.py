"""Real restic round-trip tests (skipped when restic is not installed)."""

import shutil
import uuid

import pytest

from mcagent.backups import Restic
from mcagent.config import Settings

pytestmark = pytest.mark.skipif(shutil.which("restic") is None, reason="restic binary not installed")


@pytest.fixture
def env(tmp_path):
    settings = Settings(data_root=tmp_path / "data", restic_password="test-password-123")
    iid = uuid.uuid4()
    inst = settings.instances_dir / str(iid)
    (inst / "world" / "region").mkdir(parents=True)
    (inst / "server.properties").write_text("motd=original\n")
    (inst / "world" / "level.dat").write_bytes(b"LEVEL-ORIGINAL")
    (inst / "world" / "region" / "r.0.0.mca").write_bytes(b"\x01" * 4096)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    return settings, iid, inst


def test_backup_ls_dump_restore_path(env, tmp_path):
    settings, iid, inst = env
    r = Restic(settings, iid)
    summary = r.backup(inst, [f"instance:{iid}"], [])
    snap = summary["snapshot_id"]

    root = r.ls(snap, [])
    names = {e["name"]: e["type"] for e in root}
    assert names == {"world": "directory", "server.properties": "file"}
    world = r.ls(snap, ["world"])
    assert {e["path"] for e in world} == {"world/level.dat", "world/region"}

    proc = r.dump(snap, ["server.properties"])
    data = proc.stdout.read()
    proc.wait()
    assert data == b"motd=original\n"

    (inst / "world" / "level.dat").write_bytes(b"CORRUPTED")
    restored = r.restore_path(snap, ["world", "level.dat"], tmp_path / "staging")
    assert restored.read_bytes() == b"LEVEL-ORIGINAL"

    with pytest.raises(Exception):
        r.restore_path(snap, ["does-not-exist"], tmp_path / "staging2")

    snaps = r.snapshots()
    assert len(snaps) == 1 and snaps[0]["id"].startswith(snap[:8])
    r.forget([snap])
    assert r.snapshots() == []
