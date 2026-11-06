"""btrfs snapshots end to end through the panel against a REAL, TLS-pinned node agent.

Skipped unless the btrfs node is configured, e.g. (sandbox values)::

    MCPANEL_IT_BTRFS_AGENT_URL=https://127.0.0.1:8701 MCPANEL_IT_BTRFS_TOKEN=btrfs-node-token \\
    MCPANEL_IT_BTRFS_FINGERPRINT="$(cd ../agent && AGENT_DATA_ROOT=/srv/btrfs-test/data AGENT_TLS=auto \\
        uv run python -m mcagent fingerprint)" uv run pytest -q -m integration tests/test_integration_snapshots.py

``MCPANEL_IT_BTRFS_DATA_ROOT`` is that agent's data root as seen from this machine (default
``/srv/btrfs-test/data``; set it empty to skip the on-disk checks). No Minecraft server is
started: the server is installed stopped and files are written through the agent file API,
so the snapshot/rollback/delete job handlers, the pinned TLS transport and btrfs are what
is exercised.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from .integration_support import InstanceTracker, Timer, make_node, run_job

AGENT_URL = os.environ.get("MCPANEL_IT_BTRFS_AGENT_URL", "")
TOKEN = os.environ.get("MCPANEL_IT_BTRFS_TOKEN", "")
FINGERPRINT = os.environ.get("MCPANEL_IT_BTRFS_FINGERPRINT", "")
DATA_ROOT = os.environ.get("MCPANEL_IT_BTRFS_DATA_ROOT", "/srv/btrfs-test/data")
PORTS = (25800, 25820)  # never published: these servers are not started

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (AGENT_URL and TOKEN and FINGERPRINT),
        reason="set MCPANEL_IT_BTRFS_AGENT_URL, MCPANEL_IT_BTRFS_TOKEN and MCPANEL_IT_BTRFS_FINGERPRINT",
    ),
    pytest.mark.django_db(transaction=True),
]


def _is_subvolume(path: Path) -> bool:
    return path.stat().st_ino == 256  # btrfs subvolume roots always have inode 256


def _purge_leftover_snapshots(instance_id: uuid.UUID) -> None:
    """Test cleanup only: remove snapshot subvolumes an agent left behind for our instance."""
    snap_dir = Path(DATA_ROOT, "snapshots", str(instance_id)) if DATA_ROOT else None
    if snap_dir is None or not snap_dir.is_dir() or not shutil.which("btrfs"):
        return
    for entry in snap_dir.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            subprocess.run(["btrfs", "subvolume", "delete", str(entry)], capture_output=True, check=False)
        else:
            entry.unlink(missing_ok=True)
    try:
        snap_dir.rmdir()
    except OSError as exc:
        print(f"[it] could not remove {snap_dir}: {exc}", flush=True)


@pytest.fixture
def btrfs_node(transactional_db: Any) -> Any:
    return make_node("it-btrfs-node", AGENT_URL, TOKEN, fingerprint=FINGERPRINT, ports=PORTS)


@pytest.fixture
def tracker(transactional_db: Any) -> Any:
    t = InstanceTracker(DATA_ROOT)
    yield t
    t.cleanup()
    for instance_id, _node in t.created:
        _purge_leftover_snapshots(instance_id)


def test_pinned_tls_node(btrfs_node):
    """The pin is enforced: the right fingerprint stores the certificate, a wrong one is refused."""
    from apps.nodes import tls
    from apps.nodes.client import AgentClient
    from apps.nodes.models import Node

    btrfs_node.refresh_from_db()
    assert tls.transport(btrfs_node) == "tls_pinned"
    assert tls.fingerprint_of_pem(btrfs_node.tls_cert_pem) == tls.normalize_fingerprint(FINGERPRINT)
    assert AgentClient(btrfs_node).snapshots(uuid.uuid4())["capability"]["fstype"] == "btrfs"

    wrong = Node(name="it-wrong-pin", agent_url=AGENT_URL, agent_token=TOKEN, use_settings_token=False)
    wrong.tls_fingerprint = "00" * 32
    with pytest.raises(tls.AgentTlsError) as refused:
        AgentClient(wrong).health()  # refused after the bare handshake, before the token is sent
    assert refused.value.error_code == "agent_tls_mismatch"
    assert wrong.tls_cert_pem == ""


def test_snapshot_rollback_and_delete(btrfs_node, tracker):
    from apps.nodes.client import AgentClient, AgentError

    timer = Timer()
    client = AgentClient(btrfs_node)
    inst = tracker.create(btrfs_node, "snap", PORTS)
    iid = inst.id
    try:
        # --- the install job creates the server folder as a btrfs subvolume --------------------
        run_job("install", inst, start=False, server_properties={"motd": "it snapshots"})
        listing = client.snapshots(iid)  # what GET /api/instances/<id>/snapshots returns
        assert listing["capability"]["available"] and listing["capability"]["instance_is_subvolume"], listing
        assert listing["snapshots"] == []
        inst_dir = tracker.instance_dir(inst)
        if inst_dir:
            assert _is_subvolume(inst_dir)
        client.mkdir(iid, "world", parents=True)
        client.write_text(iid, "world/marker.txt", "before snapshot")
        timer.mark("install (stopped) + marker written")

        # --- snapshot ------------------------------------------------------------------------
        job = run_job("snapshot_create", inst, label="it before change", consistency="auto", kind="manual")
        name = job.result["name"]
        assert name.endswith("-it-before-change"), name
        assert job.result["consistent"] is True and job.result["warnings"] == []
        timer.mark("snapshot_create job")

        # --- modify: change, add and delete files -------------------------------------------------
        client.write_text(iid, "world/marker.txt", "changed after snapshot")
        client.write_text(iid, "added-later.txt", "must disappear on rollback")
        client.delete_files(iid, ["server.properties"])

        # --- rollback (takes a safety snapshot first) -------------------------------------------
        job = run_job("snapshot_restore", inst, name=name)
        assert job.result == {"restored": name, "restarted": False}
        timer.mark("snapshot_restore job (safety snapshot + rollback)")
        assert client.read_text(iid, "world/marker.txt")["content"] == "before snapshot"
        assert "motd=it snapshots" in client.read_text(iid, "server.properties")["content"]
        with pytest.raises(AgentError) as gone:
            client.stat_file(iid, "added-later.txt")
        assert gone.value.agent_status == 404
        snapshots = client.snapshots(iid)
        assert snapshots["capability"]["instance_is_subvolume"] is True  # still snapshot-able after the swap
        by_kind = {s["kind"]: s for s in snapshots["snapshots"]}
        assert sorted(by_kind) == ["manual", "pre_restore"], snapshots["snapshots"]
        safety = by_kind["pre_restore"]
        assert safety["label"].startswith(f"before rollback to {name}"[:40])
        if inst_dir:
            assert _is_subvolume(inst_dir)
            assert not list(inst_dir.parent.glob(f"{iid}.mcpanel-*")), "rollback left temporary subvolumes"
            safety_dir = tracker.path("snapshots", str(iid), safety["name"])
            assert (safety_dir / "world" / "marker.txt").read_text() == "changed after snapshot"
            assert (safety_dir / "added-later.txt").exists()

        # --- delete both snapshots through the panel ----------------------------------------------
        for snap in snapshots["snapshots"]:
            run_job("snapshot_delete", inst, name=snap["name"])
        assert client.snapshots(iid)["snapshots"] == []
        snap_dir = tracker.path("snapshots", str(iid))
        if snap_dir and snap_dir.exists():
            assert [p.name for p in snap_dir.iterdir()] == []
        timer.mark("snapshot_delete jobs")

        run_job("delete", inst)
        assert client.status(iid) == {"exists": False, "operation": None}
        if inst_dir:
            assert not inst_dir.exists()
        timer.mark("delete")
    finally:
        timer.summary()


def test_deleting_a_server_removes_its_snapshots(btrfs_node, tracker):
    """Regression: the agent's delete used to orphan read-only snapshot subvolumes."""
    if not DATA_ROOT:
        pytest.skip("needs MCPANEL_IT_BTRFS_DATA_ROOT to inspect the snapshot folder")
    inst = tracker.create(btrfs_node, "snapdel", PORTS)
    run_job("install", inst, start=False)
    name = run_job("snapshot_create", inst, label="it left behind").result["name"]
    snap_dir = tracker.path("snapshots", str(inst.id))
    assert snap_dir and _is_subvolume(snap_dir / name)
    run_job("delete", inst)
    assert not tracker.instance_dir(inst).exists()
    leftover = sorted(p.name for p in snap_dir.iterdir()) if snap_dir.exists() else []
    assert not snap_dir.exists(), f"deleting the server left its snapshots behind: {leftover}"
