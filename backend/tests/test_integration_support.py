"""The helpers of the integration tests themselves (run in the normal suite, against the fake agent)."""

from __future__ import annotations

import importlib
import uuid
from pathlib import Path

import pytest

from . import integration_support

pytestmark = pytest.mark.django_db


def test_test_servers_are_published_on_loopback_by_default(node, fake_agent, monkeypatch):
    """The model's default is 0.0.0.0: a test server would be reachable from the LAN while it runs."""
    monkeypatch.delenv("MCPANEL_IT_BIND_IP", raising=False)
    support = importlib.reload(integration_support)
    try:
        assert support.BIND_IP == "127.0.0.1"
        tracker = support.InstanceTracker("")
        inst = tracker.create(node, "bind", (25900, 25950))
        assert inst.host_ip == "127.0.0.1"
        from apps.instances.spec import build_spec

        assert [p["host_ip"] for p in build_spec(inst)["ports"]] == ["127.0.0.1"]
        # a test can still ask for something else
        assert tracker.create(node, "bind", (25900, 25950), host_ip="0.0.0.0").host_ip == "0.0.0.0"
    finally:
        importlib.reload(integration_support)


@pytest.mark.parametrize(("value", "expected"), [("192.168.1.50", "192.168.1.50"), ("", "127.0.0.1")])
def test_bind_address_of_test_servers_can_be_overridden(value, expected, node, fake_agent, monkeypatch):
    monkeypatch.setenv("MCPANEL_IT_BIND_IP", value)
    support = importlib.reload(integration_support)
    try:
        assert support.InstanceTracker("").create(node, "bind", (25900, 25950)).host_ip == expected
    finally:
        monkeypatch.delenv("MCPANEL_IT_BIND_IP")
        importlib.reload(integration_support)


def test_backup_repository_path_comes_from_the_agent(node, fake_agent, monkeypatch):
    """With a separate backup directory (AGENT_BACKUP_ROOT) it is not <data root>/backups."""
    iid = uuid.uuid4()
    info = {**fake_agent.info(), "storage": {"backups": {"path": "/mnt/disk3/mcpanel-backups"}}}
    monkeypatch.setattr(fake_agent, "info", lambda: info)
    fake_agent.install(monkeypatch)  # the client methods are bound when the fake is installed
    assert integration_support.backup_repo_dir(node, iid) == Path("/mnt/disk3/mcpanel-backups/restic") / str(iid)
