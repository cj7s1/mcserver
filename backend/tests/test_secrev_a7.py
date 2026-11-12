"""A-7 of the security review of 2026-11-11: a notification when a node's disk space runs low.

The node agent reports the free space of its server and backup storage in GET /v1/info (``storage``); the
periodic task ``apps.nodes.tasks.check_disk_space`` compares it with the system setting ``low_disk_space_gb``.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.core.models import SystemSetting
from apps.nodes.tasks import check_disk_space
from apps.notifications.models import EVENTS, NotificationChannel

pytestmark = pytest.mark.django_db
GB = 1024**3


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch, node: Any) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    monkeypatch.setattr("apps.notifications.service.deliver.delay", lambda *a: out.append(a))
    NotificationChannel.objects.create(name="ops", kind="webhook", url="https://hooks.example/x", events=["node.low_disk_space"])
    return out


def _storage(fake_agent: Any, free_gb: float, *, backups_gb: float | None = None, same: bool = False) -> None:
    def entry(free: float, source: str) -> dict[str, Any]:
        return {
            "path": f"/data/{source}",
            "source": source,
            "mount_point": f"/mnt/{source}",
            "total_bytes": 500 * GB,
            "free_bytes": int(free * GB),
        }

    fake_agent.info_extra = {
        "storage": {
            "instances": entry(free_gb, "cache"),
            "backups": entry(free_gb if backups_gb is None else backups_gb, "cache" if same else "array"),
        }
    }


def test_the_event_is_offered(clients: Any) -> None:
    assert "node.low_disk_space" in EVENTS
    names = clients["owner"].get("/api/notifications/events").json()
    assert "node.low_disk_space" in str(names)


def test_low_space_is_told_once_and_again_after_it_recovered(fake_agent: Any, sent: list[Any]) -> None:
    _storage(fake_agent, 5, backups_gb=300)
    assert check_disk_space() == 1
    event, title, message, data = sent[-1][1], sent[-1][2], sent[-1][3], sent[-1][4]
    assert event == "node.low_disk_space" and "server storage" in message and "5.0 GB free" in message
    assert data["storage"] == "instances" and data["threshold_gb"] == 20 and "Low disk space" in title
    assert check_disk_space() == 0 and len(sent) == 1  # not again every 15 minutes
    _storage(fake_agent, 50, backups_gb=300)
    assert check_disk_space() == 0
    _storage(fake_agent, 3, backups_gb=2)
    assert check_disk_space() == 2 and len(sent) == 3  # both storages, each once


def test_one_filesystem_is_told_once(fake_agent: Any, sent: list[Any]) -> None:
    _storage(fake_agent, 5, same=True)
    assert check_disk_space() == 1 and len(sent) == 1


def test_the_threshold_setting(fake_agent: Any, sent: list[Any], clients: Any) -> None:
    _storage(fake_agent, 30, backups_gb=300)
    assert check_disk_space() == 0
    resp = clients["owner"].patch("/api/system/settings", {"values": {"low_disk_space_gb": 50}}, format="json")
    assert resp.status_code == 200, resp.content
    assert check_disk_space() == 1
    SystemSetting.set("low_disk_space_gb", 0)  # off
    _storage(fake_agent, 1)
    assert check_disk_space() == 0
    bad = clients["owner"].patch("/api/system/settings", {"values": {"low_disk_space_gb": -1}}, format="json")
    assert bad.status_code == 400


def test_an_agent_without_storage_data_or_unreachable_is_skipped(fake_agent: Any, sent: list[Any], monkeypatch: Any) -> None:
    from apps.nodes.client import AgentClient, AgentUnavailable

    fake_agent.info_extra = {}
    assert check_disk_space() == 0

    def down(self: Any) -> Any:
        raise AgentUnavailable("down")

    monkeypatch.setattr(AgentClient, "info", down)
    assert check_disk_space() == 0 and sent == []


def test_the_beat_schedule_runs_it(settings: Any) -> None:
    entry = settings.CELERY_BEAT_SCHEDULE["node-disk-space"]
    assert entry["task"] == "apps.nodes.tasks.check_disk_space" and entry["options"]["expires"] < entry["schedule"]
