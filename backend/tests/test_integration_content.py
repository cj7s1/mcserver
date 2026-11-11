"""Plugin install on a REAL Paper server through the panel's content service (live Modrinth).

Skipped unless ``MCPANEL_IT_AGENT_URL`` is set, e.g.::

    MCPANEL_IT_AGENT_URL=http://127.0.0.1:8700 uv run pytest -q -m integration tests/test_integration_content.py -s

Needs internet access from the panel (Modrinth API) and from the agent and the server
container (Modrinth CDN, Paper and Mojang downloads). Shares ``MCPANEL_IT_AGENT_TOKEN`` /
``MCPANEL_IT_AGENT_DATA_ROOT`` / ``MCPANEL_IT_READY_TIMEOUT`` with
``test_integration_agent.py``; the game port comes from 25800-25820.

Flow: Paper 1.21.1 (1024 MB) is installed and started, a small plugin with no dependencies
(Chunky) is installed with ``apps.content.service.install`` (the agent downloads it from
Modrinth's CDN and verifies the published hashes), the server is restarted and must load it,
then the plugin is uninstalled and the server stopped and deleted. Everything is removed
even when an assertion fails.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from .integration_support import (
    READY_TIMEOUT,
    InstanceTracker,
    Timer,
    container_names,
    it_ports,
    make_node,
    rcon,
    run_job,
    wait_status,
)

AGENT_URL = os.environ.get("MCPANEL_IT_AGENT_URL", "")
AGENT_TOKEN = os.environ.get("MCPANEL_IT_AGENT_TOKEN", "dev-agent-token")
DATA_ROOT = os.environ.get("MCPANEL_IT_AGENT_DATA_ROOT", "/srv/mcpanel-dev")
PORTS = it_ports((25800, 25820))
PLUGIN_SLUG = "chunky"  # ~300 KB, no dependencies, needs no network at runtime
PLUGIN_NAME = "Chunky"

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


def test_paper_plugin_install_restart_uninstall(it_node, tracker):
    from apps.content import service
    from apps.instances.models import Instance
    from apps.modpacks.providers import PROVIDERS
    from apps.nodes.client import AgentClient, AgentError

    timer = Timer()
    client = AgentClient(it_node)
    inst = tracker.create(it_node, "paper", PORTS, server_type="PAPER", minecraft_version="1.21.1")
    iid = inst.id
    try:
        # --- Paper server up -----------------------------------------------------------------
        run_job(
            "install",
            inst,
            start=True,
            server_properties={
                "motd": "MCPanel content integration test",
                "level-type": "minecraft:flat",
                "generate-structures": "false",
                "view-distance": "4",
                "simulation-distance": "4",
            },
        )
        inst = wait_status(inst, "running", READY_TIMEOUT)
        timer.mark("install + first start until healthy (downloads Paper, generates world)")

        # --- install through the content service --------------------------------------------------
        result = service.install(inst, project_id=PLUGIN_SLUG)
        [item] = result["installed"]
        filename = item["filename"]
        assert item["dependency"] is False and filename.endswith(".jar"), item
        assert result["missing_dependencies"] == [] and result["restart_required"] is True, result
        timer.mark(f"content install ({filename})")

        inventory = service.inventory(inst)
        assert inventory["kind"] == "plugins" and inventory["identification"]["available"] is True
        [entry] = [f for f in inventory["files"] if f["filename"] == filename]
        assert entry["source"] == "modrinth" and entry["project"]["slug"] == PLUGIN_SLUG, entry
        assert entry["version"]["id"] == item["version_id"]
        assert entry["compatibility"] == {"loader": True, "minecraft": True}
        release = PROVIDERS["modrinth"].release(item["version_id"])
        assert entry["sha1"] == release["file"]["sha1"]  # the agent's own hash of the bytes on disk

        # the agent refuses a download whose bytes do not match the expected hash
        with pytest.raises(AgentError) as tampered:
            client.fetch_file(iid, url=release["file"]["url"], path="plugins/it-tampered.jar", sha1="0" * 40)
        assert tampered.value.error_code == "hash_mismatch"
        with pytest.raises(AgentError) as absent:
            client.stat_file(iid, "plugins/it-tampered.jar")
        assert absent.value.agent_status == 404
        timer.mark("inventory + hash checks")

        # --- restart: the server loads the plugin ------------------------------------------------
        run_job("restart", inst)
        inst = wait_status(inst, "running", READY_TIMEOUT)
        timer.mark("restart until healthy")
        assert PLUGIN_NAME in rcon(inst, "plugins")
        log = "\n".join(client.logs(iid, tail=2000))
        assert f"Enabling {PLUGIN_NAME}" in log, "the plugin was listed but not enabled"
        timer.mark("plugin loaded (RCON plugins + log)")

        # --- uninstall, stop, delete ------------------------------------------------------------------
        inst = Instance.objects.select_related("node").get(pk=iid)
        assert service.delete(inst, filename) == {"deleted": filename, "restart_required": True}
        with pytest.raises(AgentError) as removed:
            client.stat_file(iid, f"plugins/{filename}")
        assert removed.value.agent_status == 404
        assert [f["filename"] for f in service.inventory(inst)["files"]] == []

        run_job("stop", inst)
        inst = wait_status(inst, "stopped", 120)
        timer.mark("uninstall + stop")
        run_job("delete", inst)
        assert not Instance.objects.filter(pk=iid).exists()
        assert client.status(iid) == {"exists": False, "operation": None}
        if DATA_ROOT:
            assert not tracker.instance_dir(inst).exists()
        assert container_names(iid) in ("", None)
        timer.mark("delete")
    finally:
        timer.summary()
