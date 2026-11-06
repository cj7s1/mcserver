"""restic backup browser + single-path restore through the panel against a REAL agent.

Skipped unless ``MCPANEL_IT_AGENT_URL`` is set (and when the agent has no restic), e.g.::

    MCPANEL_IT_AGENT_URL=http://127.0.0.1:8700 uv run pytest -q -m integration tests/test_integration_backup_browse.py

Shares ``MCPANEL_IT_AGENT_TOKEN`` / ``MCPANEL_IT_AGENT_DATA_ROOT`` with
``test_integration_agent.py``. No Minecraft server is started: the server is installed
stopped, its files are written through the agent file API, backed up by the backup job,
then browsed, downloaded and partially restored through the panel API views
(``/backups/<id>/files``, ``/download``, ``/restore-path``) as the backup browser does.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from .integration_support import (
    InstanceTracker,
    Timer,
    make_node,
    restic_available,
    run_job,
    runtime_owner,
    wrong_owner,
)

AGENT_URL = os.environ.get("MCPANEL_IT_AGENT_URL", "")
AGENT_TOKEN = os.environ.get("MCPANEL_IT_AGENT_TOKEN", "dev-agent-token")
DATA_ROOT = os.environ.get("MCPANEL_IT_AGENT_DATA_ROOT", "/srv/mcpanel-dev")
PORTS = (25800, 25820)  # never published: the server is not started

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not AGENT_URL, reason="set MCPANEL_IT_AGENT_URL to run against a real agent"),
    pytest.mark.django_db(transaction=True),
]


@pytest.fixture
def it_node(transactional_db: Any) -> Any:
    node = make_node("integration-agent", AGENT_URL, AGENT_TOKEN, ports=PORTS)
    if not restic_available(node):
        pytest.skip("restic is not installed on the agent")
    return node


@pytest.fixture
def tracker(transactional_db: Any) -> Any:
    t = InstanceTracker(DATA_ROOT)
    yield t
    t.cleanup()


@pytest.fixture
def owner_api(make_user: Any, client_for: Any) -> Any:
    return client_for(make_user("it-owner", owner=True))


def _entries(api: Any, url: str, path: str = "") -> dict[str, dict[str, Any]]:
    resp = api.get(url, {"path": path} if path else {})
    assert resp.status_code == 200, resp.content
    assert resp.json()["path"] == path
    return {e["name"]: e for e in resp.json()["entries"]}


def _restore_path(api: Any, url: str, path: str) -> Any:
    from apps.jobs.models import Job, JobStatus

    resp = api.post(url, {"path": path, "confirm": True}, format="json")
    assert resp.status_code == 202, resp.content
    job = Job.objects.get(pk=resp.json()["id"])  # eager Celery: already ran
    assert job.status == JobStatus.SUCCEEDED, f"restore_path {job.status}: [{job.error_code}] {job.error}"
    return job


def test_backup_browse_download_and_restore_paths(it_node, tracker, owner_api):
    from apps.audit.models import AuditEvent
    from apps.backups.models import Backup, BackupStatus
    from apps.nodes.client import AgentClient, AgentError

    timer = Timer()
    client = AgentClient(it_node)
    inst = tracker.create(it_node, "backup", PORTS)
    iid = inst.id
    base = f"/api/instances/{iid}/backups"
    try:
        # --- a stopped server with files written through the agent file API -------------------
        run_job("install", inst, start=False, server_properties={"motd": "it backup browser"})
        region = os.urandom(96 * 1024)  # binary and incompressible
        client.mkdir(iid, "world/region", parents=True)
        client.mkdir(iid, "config", parents=True)
        client.write_text(iid, "world/marker.txt", "as backed up")
        client.write_text(iid, "world/sibling.txt", "sibling v1")
        client.write_text(iid, "config/settings.yml", "difficulty: hard\n")
        client.upload(iid, "world/region/r.0.0.mca", region)
        timer.mark("install (stopped) + files written")

        job = run_job("backup", inst, name="it browse", kind="manual", consistency="auto")
        backup = Backup.objects.get(pk=job.result["backup_id"])
        assert backup.status == BackupStatus.SUCCEEDED and backup.snapshot_id
        assert backup.consistent is True, backup.warnings  # stopped server: nothing to coordinate
        assert backup.files_count and backup.files_count >= 5
        timer.mark("backup job")

        # --- browse + download ----------------------------------------------------------------
        top = _entries(owner_api, f"{base}/{backup.id}/files")
        assert {"world", "config", "server.properties"} <= set(top), sorted(top)
        assert top["world"]["type"] == "directory" and top["server.properties"]["type"] == "file"
        world = _entries(owner_api, f"{base}/{backup.id}/files", "world")
        assert {"marker.txt", "sibling.txt", "region"} <= set(world), sorted(world)
        assert world["marker.txt"]["path"] == "world/marker.txt"
        regions = _entries(owner_api, f"{base}/{backup.id}/files", "world/region")
        assert regions["r.0.0.mca"]["size"] == len(region)
        resp = owner_api.get(f"{base}/{backup.id}/download", {"path": "world/region/r.0.0.mca"})
        assert resp.status_code == 200
        assert b"".join(resp.streaming_content) == region
        assert "r.0.0.mca" in resp["Content-Disposition"]
        timer.mark("browse (3 levels) + download")

        # --- change things after the backup -----------------------------------------------------
        client.write_text(iid, "world/marker.txt", "changed later")
        client.write_text(iid, "world/sibling.txt", "sibling v2 (must be kept)")
        client.delete_files(iid, ["config"], recursive=True)

        # --- restore one file: only that file changes ---------------------------------------------
        resp = owner_api.post(f"{base}/{backup.id}/restore-path", {"path": "world/marker.txt"}, format="json")
        assert resp.status_code == 400 and resp.json()["error"]["code"] == "confirmation_required"
        job = _restore_path(owner_api, f"{base}/{backup.id}/restore-path", "world/marker.txt")
        assert job.result == {"restored": "world/marker.txt", "backup": str(backup.id), "restarted": False}
        assert client.read_text(iid, "world/marker.txt")["content"] == "as backed up"
        assert client.read_text(iid, "world/sibling.txt")["content"] == "sibling v2 (must be kept)"
        with pytest.raises(AgentError) as still_deleted:
            client.stat_file(iid, "config")
        assert still_deleted.value.agent_status == 404

        # --- restore a deleted folder ---------------------------------------------------------------
        _restore_path(owner_api, f"{base}/{backup.id}/restore-path", "config")
        assert client.read_text(iid, "config/settings.yml")["content"] == "difficulty: hard\n"
        timer.mark("restore-path jobs (file + folder)")

        leftovers = [e["name"] for e in client.list_files(iid, "world")["entries"] if e["name"].startswith(".mcpanel")]
        assert leftovers == [], "restore-path left the replaced file behind"
        inst_dir = tracker.instance_dir(inst)
        if inst_dir:
            assert wrong_owner([inst_dir / "world" / "marker.txt", inst_dir / "config"], runtime_owner(it_node)) == []
        actions = list(AuditEvent.objects.filter(instance_id=iid).values_list("action", flat=True))
        assert actions.count("backup.restore_path") == 2 and "backup.file_download" in actions, actions

        run_job("delete", inst)
        if DATA_ROOT:
            assert not tracker.path("backups", "restic", str(iid)).exists()
        timer.mark("delete")
    finally:
        timer.summary()
