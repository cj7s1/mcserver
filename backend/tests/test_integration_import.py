"""Import an existing server from a zip through the panel's import endpoint, against a REAL agent.

Skipped unless ``MCPANEL_IT_AGENT_URL`` is set, e.g.::

    MCPANEL_IT_AGENT_URL=http://127.0.0.1:8700 uv run pytest -q -m integration tests/test_integration_import.py

Shares ``MCPANEL_IT_AGENT_TOKEN`` / ``MCPANEL_IT_AGENT_DATA_ROOT`` with
``test_integration_agent.py`` (the ownership checks need the data root). No Minecraft
server is started. ``POST /api/instances/<id>/import`` streams the upload to the agent and
runs the ``extract_archive`` job, which extracts it with the agent's confined writer.
"""

from __future__ import annotations

import gzip
import io
import os
import stat
import zipfile
from typing import Any

import pytest

from .integration_support import InstanceTracker, Timer, disk, it_ports, make_node, run_job, runtime_owner, wrong_owner

AGENT_URL = os.environ.get("MCPANEL_IT_AGENT_URL", "")
AGENT_TOKEN = os.environ.get("MCPANEL_IT_AGENT_TOKEN", "dev-agent-token")
DATA_ROOT = os.environ.get("MCPANEL_IT_AGENT_DATA_ROOT", "/srv/mcpanel-dev")
PORTS = it_ports((25800, 25820))  # never published: the server is not started
TOP = "Old Server"  # the archive's single top-level folder (stripped on import)

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


@pytest.fixture
def owner_api(make_user: Any, client_for: Any) -> Any:
    return client_for(make_user("it-owner", owner=True))


def _server_zip(files: dict[str, bytes]) -> bytes:
    """A zipped server folder as people upload it, plus two entries that must be skipped."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{TOP}/", b"")
        zf.writestr(f"{TOP}/world/datapacks/", b"")  # empty directory
        for name, data in files.items():
            zf.writestr(f"{TOP}/{name}", data)
        zf.writestr("../escape.txt", b"zip-slip: must never be written")
        link = zipfile.ZipInfo(f"{TOP}/evil-link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(link, "/etc/passwd")
    return buf.getvalue()


def test_import_server_archive(it_node, tracker, owner_api):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from apps.audit.models import AuditEvent
    from apps.jobs.models import Job, JobStatus
    from apps.nodes.client import AgentClient, AgentError

    timer = Timer()
    client = AgentClient(it_node)
    inst = tracker.create(it_node, "import", PORTS)
    iid = inst.id
    files = {
        "server.properties": b"motd=Imported by the integration test\nlevel-name=world\nmax-players=7\n",
        "world/level.dat": gzip.compress(b"\x0a\x00\x00" + os.urandom(512)),  # gzipped NBT-like blob
        "world/region/r.0.0.mca": os.urandom(64 * 1024),
        "ops.json": b"[]\n",
    }
    try:
        # the import wizard creates the server without starting it, then uploads the archive
        run_job("install", inst, start=False)
        timer.mark("install (stopped)")
        upload = SimpleUploadedFile("old-server.zip", _server_zip(files), content_type="application/zip")
        resp = owner_api.post(f"/api/instances/{iid}/import", {"file": upload}, format="multipart")
        assert resp.status_code == 202, resp.content
        job = Job.objects.get(pk=resp.json()["id"])  # eager Celery: already ran
        assert job.kind == "extract_archive"
        assert job.status == JobStatus.SUCCEEDED, f"import {job.status}: [{job.error_code}] {job.error}"
        result = job.result
        assert result["stripped_prefix"] == TOP
        assert result["files"] == len(files) and result["existing_count"] == 0, result
        assert result["skipped_count"] == 2, result["skipped"]  # ../escape.txt and the symlink
        timer.mark("import endpoint (upload + extract job)")

        # --- files are at the server root, byte for byte -------------------------------------------
        for name, data in files.items():
            chunks, _length, _resp = client.download(iid, name)
            assert b"".join(chunks) == data, name
        assert client.stat_file(iid, "world/datapacks")["type"] == "directory"
        root_names = {e["name"] for e in client.list_files(iid, "")["entries"]}
        assert {"server.properties", "world", "ops.json"} <= root_names
        assert TOP not in root_names and "evil-link" not in root_names
        assert not [n for n in root_names if n.startswith(".mcpanel-import-")], "the uploaded archive was kept"
        with pytest.raises(AgentError) as no_link:
            client.stat_file(iid, "evil-link")
        assert no_link.value.agent_status == 404
        props = owner_api.get(f"/api/instances/{iid}/properties").json()
        assert props["exists"] and props["values"]["max-players"] in (7, "7")
        inst.refresh_from_db()
        assert inst.installed_at is not None  # imported servers count as installed
        assert AuditEvent.objects.filter(instance_id=iid, action="instance.imported").exists()

        # --- on disk: owned by the runtime user, nothing outside the folder ---------------------------
        inst_dir = tracker.instance_dir(inst)
        if inst_dir:
            assert wrong_owner([inst_dir], runtime_owner(it_node)) == []
            # (the data root is root-only on a real host: through AgentDisk, like the other checks)
            assert not disk.exists(inst_dir.parent / "escape.txt")
            assert not disk.exists(inst_dir.parent.parent / "escape.txt")
        else:
            print("[it] MCPANEL_IT_AGENT_DATA_ROOT empty: ownership check SKIPPED", flush=True)
        timer.mark("verified files")

        run_job("delete", inst)
        timer.mark("delete")
    finally:
        timer.summary()
