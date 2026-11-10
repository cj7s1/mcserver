"""End-to-end test against a REAL node agent + Docker daemon (scenario 1-3, 7).

Skipped unless ``MCPANEL_IT_AGENT_URL`` is set, e.g.::

    MCPANEL_IT_AGENT_URL=http://127.0.0.1:8700 uv run pytest -q -m integration -s

Optional: ``MCPANEL_IT_AGENT_TOKEN`` (default ``dev-agent-token``),
``MCPANEL_IT_AGENT_DATA_ROOT`` (the agent's data root as seen from this machine, default
``/srv/mcpanel-dev``; set it empty to skip on-disk checks), ``MCPANEL_IT_READY_TIMEOUT``
(seconds, default 360), ``MCPANEL_IT_BIND_IP`` (host address the game port is published on,
default ``127.0.0.1``). Game ports are taken from 25600-25650.

The test drives the real job handlers (install / stop / start / backup / restore /
delete) through the job runner, so it exercises the panel's code paths, the agent API,
Docker, the itzg runtime, RCON and restic together. Everything it creates is removed
even when an assertion fails.
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest

from .integration_support import (
    READY_TIMEOUT,
    InstanceTracker,
    Timer,
    backup_repo_dir,
    container_names,
    disk,
    make_node,
    rcon,
    restic_available,
    run_job,
    wait_status,
)

AGENT_URL = os.environ.get("MCPANEL_IT_AGENT_URL", "")
AGENT_TOKEN = os.environ.get("MCPANEL_IT_AGENT_TOKEN", "dev-agent-token")
DATA_ROOT = os.environ.get("MCPANEL_IT_AGENT_DATA_ROOT", "/srv/mcpanel-dev")
PORTS = (25600, 25650)
BLOCK = (0, 100, 0)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not AGENT_URL, reason="set MCPANEL_IT_AGENT_URL to run against a real agent"),
    pytest.mark.django_db(transaction=True),
]


@pytest.fixture
def it_node(transactional_db: Any) -> Any:
    return make_node("integration-agent", AGENT_URL, AGENT_TOKEN, ports=PORTS)


@pytest.fixture
def tracker(transactional_db: Any) -> Any:
    t = InstanceTracker(DATA_ROOT)
    yield t
    t.cleanup()


def _block_is(instance: Any, block: str, *, attempts: int = 10) -> bool:
    x, y, z = BLOCK
    out = ""
    for _ in range(attempts):
        out = rcon(instance, f"execute if block {x} {y} {z} minecraft:{block}")
        if "Test passed" in out:
            return True
        if "Test failed" in out:
            return False
        time.sleep(3)  # e.g. "That position is not loaded" right after start
    raise AssertionError(f"unexpected answer to block test: {out!r}")


def _set_block(instance: Any, block: str) -> None:
    x, y, z = BLOCK
    out = rcon(instance, f"setblock {x} {y} {z} minecraft:{block}")
    assert "Changed the block" in out or "Could not set the block" in out, out
    assert "Saved the game" in rcon(instance, "save-all flush")


def test_vanilla_lifecycle_and_backup_restore(it_node, tracker):
    from apps.backups.models import Backup, BackupKind, BackupStatus
    from apps.instances.models import Instance
    from apps.nodes.client import AgentClient

    timer = Timer()
    # Mojang: 1.20.4 requires Java 17 (docs/reference/version-metadata.md)
    instance = tracker.create(it_node, "vanilla", PORTS, minecraft_version="1.20.4", java_version=17)
    iid = instance.id
    print(f"[it] image {instance.image}", flush=True)
    client = AgentClient(it_node)
    try:
        # --- install + first start ------------------------------------------------------------
        run_job(
            "install",
            instance,
            start=True,
            server_properties={
                "motd": "MCPanel integration test",
                "level-type": "minecraft:flat",
                "generate-structures": "false",
                "view-distance": "4",
                "simulation-distance": "4",
                "spawn-protection": "0",
            },
        )
        status = client.status(iid)
        assert status["exists"] and status["running"]
        assert status["ports"] and status["ports"][0]["host_port"] == instance.game_port
        timer.mark("install job (container created + started)")
        inst = wait_status(instance, "running", READY_TIMEOUT)
        assert inst.installed_at is not None
        timer.mark("first start until healthy (downloads server jar, generates world)")

        # --- world change through RCON -------------------------------------------------------
        rcon(inst, "forceload add 0 0")  # keep the test chunk loaded regardless of spawn location
        _set_block(inst, "gold_block")
        assert _block_is(inst, "gold_block")
        timer.mark("RCON setblock + save")

        # --- stop / start keeps the world -----------------------------------------------------
        run_job("stop", inst)
        inst = wait_status(inst, "stopped", 120)
        # (a stopped server's container is removed some seconds after the stop: no "running" key then)
        assert not client.status(iid).get("running")
        timer.mark("stop")
        run_job("start", inst)
        inst = wait_status(inst, "running", READY_TIMEOUT)
        timer.mark("start until healthy")
        assert _block_is(inst, "gold_block"), "world change was lost across stop/start"
        timer.mark("verified block after restart")

        # --- scenario 7: backup -> change -> restore -------------------------------------------
        if restic_available(it_node):
            job = run_job("backup", inst, name="it gold", kind="manual", consistency="require")
            backup = Backup.objects.get(pk=job.result["backup_id"])
            assert backup.status == BackupStatus.SUCCEEDED and backup.snapshot_id
            assert backup.consistent is True, backup.warnings
            assert backup.files_count and backup.files_count > 10
            if DATA_ROOT:
                # the repository is where the agent says it is: the check after the delete means something
                assert disk.is_dir(backup_repo_dir(it_node, iid)), backup_repo_dir(it_node, iid)
            timer.mark("online backup (save-off/save-all/restic/save-on)")

            _set_block(inst, "diamond_block")
            assert _block_is(inst, "diamond_block")
            job = run_job("restore", inst, backup_id=str(backup.id), pre_restore_backup=True)
            assert job.result["restarted"] is True
            safety = Backup.objects.get(pk=job.result["safety_backup"])
            assert safety.kind == BackupKind.PRE_RESTORE and safety.status == BackupStatus.SUCCEEDED
            timer.mark("restore job (stop, safety backup, restore, start)")
            inst = wait_status(inst, "running", READY_TIMEOUT)
            timer.mark("start after restore until healthy")
            assert _block_is(inst, "gold_block"), "restore did not bring back the backed-up world"
            assert not _block_is(inst, "diamond_block")
            snapshot_ids = [s["id"] for s in client.list_backups(iid)]
            for snap in (backup.snapshot_id, safety.snapshot_id):
                assert any(full.startswith(snap) for full in snapshot_ids), (snap, snapshot_ids)
            timer.mark("verified restored block")
        else:
            print("[it] restic not available on the agent: backup/restore scenario SKIPPED", flush=True)

        # --- delete removes container, files and backups ----------------------------------------
        repo = backup_repo_dir(it_node, iid)  # asked before the delete; not always <data root>/backups
        run_job("delete", inst)
        assert not Instance.objects.filter(pk=iid).exists()
        assert client.status(iid) == {"exists": False, "operation": None}
        if DATA_ROOT:
            assert not disk.exists(tracker.path("instances", str(iid)))
            assert not disk.exists(repo), repo
        assert container_names(iid) in ("", None)
        timer.mark("delete")
    finally:
        timer.summary()
