"""End-to-end test against a REAL node agent + Docker daemon (scenario 1-3, 7).

Skipped unless ``MCPANEL_IT_AGENT_URL`` is set, e.g.::

    MCPANEL_IT_AGENT_URL=http://127.0.0.1:8700 uv run pytest -q -m integration -s

Optional: ``MCPANEL_IT_AGENT_TOKEN`` (default ``dev-agent-token``),
``MCPANEL_IT_AGENT_DATA_ROOT`` (the agent's data root as seen from this machine, default
``/srv/mcpanel-dev``; set it empty to skip on-disk checks), ``MCPANEL_IT_READY_TIMEOUT``
(seconds, default 360). Game ports are taken from 25600-25650.

The test drives the real job handlers (install / stop / start / backup / restore /
delete) through the job runner, so it exercises the panel's code paths, the agent API,
Docker, the itzg runtime, RCON and restic together. Everything it creates is removed
even when an assertion fails.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

AGENT_URL = os.environ.get("MCPANEL_IT_AGENT_URL", "")
AGENT_TOKEN = os.environ.get("MCPANEL_IT_AGENT_TOKEN", "dev-agent-token")
DATA_ROOT = os.environ.get("MCPANEL_IT_AGENT_DATA_ROOT", "/srv/mcpanel-dev")
READY_TIMEOUT = float(os.environ.get("MCPANEL_IT_READY_TIMEOUT", "360"))
PORTS = (25600, 25650)
BLOCK = (0, 100, 0)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not AGENT_URL, reason="set MCPANEL_IT_AGENT_URL to run against a real agent"),
    pytest.mark.django_db(transaction=True),
]


class Timer:
    def __init__(self) -> None:
        self.start = time.monotonic()
        self.marks: list[tuple[str, float]] = []
        self._last = self.start

    def mark(self, label: str) -> None:
        now = time.monotonic()
        elapsed, self._last = now - self._last, now
        self.marks.append((label, elapsed))
        print(f"[it] {label}: {elapsed:.1f}s (total {now - self.start:.1f}s)", flush=True)


def _host_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


@pytest.fixture
def it_node(db: Any) -> Any:
    from apps.core.models import SystemSetting
    from apps.nodes.client import AgentClient
    from apps.nodes.models import Node

    node = Node.objects.create(
        name="integration-agent", agent_url=AGENT_URL, agent_token=AGENT_TOKEN, use_settings_token=False, is_default=True
    )
    health = AgentClient(node).health()
    assert health["docker"] is True, f"agent at {AGENT_URL} reports Docker unavailable: {health}"
    SystemSetting.set("port_range_start", PORTS[0])
    SystemSetting.set("port_range_end", PORTS[1])
    return node


def _pick_port(node: Any) -> int:
    from apps.instances import services

    used = services.ports_in_use(node)  # panel DB + every port the Docker host publishes
    for port in range(PORTS[0], PORTS[1] + 1):
        if port not in used and _host_port_free(port):
            return port
    pytest.fail(f"no free port in {PORTS}")


def _run_job(job_kind: str, instance: Any, **params: Any) -> Any:
    from apps.jobs.models import Job, JobStatus
    from apps.jobs.runner import enqueue

    # transaction=True: on_commit fires immediately and eager Celery runs the job inline
    job = enqueue(job_kind, title=f"it {job_kind}", instance=instance, params=params)
    job = Job.objects.get(pk=job.pk)
    assert job.status == JobStatus.SUCCEEDED, f"{job_kind} job {job.status}: [{job.error_code}] {job.error}\n" + "\n".join(
        job.log[-20:]
    )
    return job


def _wait_status(instance: Any, wanted: str, timeout: float) -> Any:
    from apps.instances.reconcile import reconcile_instance
    from apps.nodes.client import AgentClient

    deadline = time.monotonic() + timeout
    seen = []
    while True:
        inst = reconcile_instance(instance.id)
        if not seen or seen[-1] != inst.status:
            seen.append(inst.status)
            print(f"[it]   status -> {inst.status} {inst.status_detail!r}", flush=True)
        if inst.status == wanted:
            return inst
        if inst.status == "failed" or time.monotonic() > deadline:
            try:
                tail = "\n".join(AgentClient(inst.node).logs(inst.id, tail=60))
            except Exception as exc:  # pragma: no cover - diagnostics only
                tail = f"<logs unavailable: {exc}>"
            pytest.fail(f"instance did not reach {wanted!r} (statuses: {seen}); container log tail:\n{tail}")
        time.sleep(3)


def _rcon(instance: Any, command: str, *, attempts: int = 20) -> str:
    from apps.core.errors import ApiError
    from apps.nodes.client import AgentClient

    last: Exception | None = None
    for _ in range(attempts):
        try:
            out = AgentClient(instance.node).command(instance.id, command)
            print(f"[it]   rcon {command!r} -> {out.strip()!r}", flush=True)
            return out
        except ApiError as exc:  # RCON listener can lag slightly behind the status ping
            last = exc
            time.sleep(3)
    raise AssertionError(f"RCON command {command!r} kept failing: {last}")


def _block_is(instance: Any, block: str, *, attempts: int = 10) -> bool:
    x, y, z = BLOCK
    out = ""
    for _ in range(attempts):
        out = _rcon(instance, f"execute if block {x} {y} {z} minecraft:{block}")
        if "Test passed" in out:
            return True
        if "Test failed" in out:
            return False
        time.sleep(3)  # e.g. "That position is not loaded" right after start
    raise AssertionError(f"unexpected answer to block test: {out!r}")


def _set_block(instance: Any, block: str) -> None:
    x, y, z = BLOCK
    out = _rcon(instance, f"setblock {x} {y} {z} minecraft:{block}")
    assert "Changed the block" in out or "Could not set the block" in out, out
    assert "Saved the game" in _rcon(instance, "save-all flush")


def _restic_available(node: Any) -> bool:
    from apps.nodes.client import AgentClient

    return bool(AgentClient(node).info().get("restic"))


def _cleanup(instance_id: uuid.UUID, node: Any) -> None:
    """Best effort: remove container, files and backup repo whatever state the test left."""
    from apps.nodes.client import AgentClient

    client = AgentClient(node)
    try:
        if client.status(instance_id).get("exists") or (
            DATA_ROOT and (Path(DATA_ROOT) / "instances" / str(instance_id)).exists()
        ):
            op = client.delete_data(instance_id)
            client.wait_operation(op["id"], timeout=300)
    except Exception as exc:
        print(f"[it] cleanup via agent failed: {exc}; falling back to docker rm", flush=True)
        subprocess.run(["docker", "rm", "-f", f"mcpanel-{instance_id.hex[:12]}"], capture_output=True, check=False)


def test_vanilla_lifecycle_and_backup_restore(it_node):
    from apps.backups.models import Backup, BackupKind, BackupStatus
    from apps.instances.models import Instance
    from apps.instances.spec import image_for
    from apps.nodes.client import AgentClient

    timer = Timer()
    port = _pick_port(it_node)
    instance = Instance.objects.create(
        name=f"it-vanilla-{uuid.uuid4().hex[:6]}",
        node=it_node,
        server_type="VANILLA",
        minecraft_version="1.20.4",
        java_version=17,  # Mojang: 1.20.4 requires Java 17 (docs/reference/version-metadata.md)
        image=image_for(17),
        memory_mb=1024,
        jvm_flags_preset="none",
        stop_timeout=60,
        game_port=port,
    )
    iid = instance.id
    print(f"[it] instance {iid} on port {port}, image {instance.image}", flush=True)
    client = AgentClient(it_node)
    try:
        # --- install + first start ------------------------------------------------------------
        _run_job(
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
        assert status["ports"] and status["ports"][0]["host_port"] == port
        timer.mark("install job (container created + started)")
        inst = _wait_status(instance, "running", READY_TIMEOUT)
        assert inst.installed_at is not None
        timer.mark("first start until healthy (downloads server jar, generates world)")

        # --- world change through RCON -------------------------------------------------------
        _rcon(inst, "forceload add 0 0")  # keep the test chunk loaded regardless of spawn location
        _set_block(inst, "gold_block")
        assert _block_is(inst, "gold_block")
        timer.mark("RCON setblock + save")

        # --- stop / start keeps the world -----------------------------------------------------
        _run_job("stop", inst)
        inst = _wait_status(inst, "stopped", 120)
        assert client.status(iid)["running"] is False
        timer.mark("stop")
        _run_job("start", inst)
        inst = _wait_status(inst, "running", READY_TIMEOUT)
        timer.mark("start until healthy")
        assert _block_is(inst, "gold_block"), "world change was lost across stop/start"
        timer.mark("verified block after restart")

        # --- scenario 7: backup -> change -> restore -------------------------------------------
        if _restic_available(it_node):
            job = _run_job("backup", inst, name="it gold", kind="manual", consistency="require")
            backup = Backup.objects.get(pk=job.result["backup_id"])
            assert backup.status == BackupStatus.SUCCEEDED and backup.snapshot_id
            assert backup.consistent is True, backup.warnings
            assert backup.files_count and backup.files_count > 10
            timer.mark("online backup (save-off/save-all/restic/save-on)")

            _set_block(inst, "diamond_block")
            assert _block_is(inst, "diamond_block")
            job = _run_job("restore", inst, backup_id=str(backup.id), pre_restore_backup=True)
            assert job.result["restarted"] is True
            safety = Backup.objects.get(pk=job.result["safety_backup"])
            assert safety.kind == BackupKind.PRE_RESTORE and safety.status == BackupStatus.SUCCEEDED
            timer.mark("restore job (stop, safety backup, restore, start)")
            inst = _wait_status(inst, "running", READY_TIMEOUT)
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
        _run_job("delete", inst)
        assert not Instance.objects.filter(pk=iid).exists()
        assert client.status(iid) == {"exists": False, "operation": None}
        if DATA_ROOT:
            assert not (Path(DATA_ROOT) / "instances" / str(iid)).exists()
            assert not (Path(DATA_ROOT) / "backups" / "restic" / str(iid)).exists()
        docker_ps = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=mcpanel-{iid.hex[:12]}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if docker_ps.returncode == 0:
            assert docker_ps.stdout.strip() == ""
        timer.mark("delete")
    finally:
        _cleanup(iid, it_node)
        print("[it] timings: " + ", ".join(f"{label} {secs:.1f}s" for label, secs in timer.marks), flush=True)
