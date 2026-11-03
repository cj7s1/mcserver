from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.instances.models import Instance
from apps.metrics.models import MetricSample, PlayerSeen
from apps.metrics.tasks import record_players, sample_all
from apps.nodes.models import Node

pytestmark = pytest.mark.django_db


@pytest.fixture
def instance():
    node = Node.objects.create(name="n1", agent_url="http://agent.invalid:8700", is_default=True)
    return Instance.objects.create(name="M", node=node, game_port=25611, image="itzg/minecraft-server:stable-java21",
                                   status="running")


def test_sample_all_records_stats_and_players(instance, monkeypatch):
    monkeypatch.setattr("apps.nodes.client.AgentClient.stats", lambda self, iid: {
        "running": True, "cpu_percent": 12.5, "memory_bytes": 1000, "memory_limit_bytes": 2000,
        "network_rx_bytes": 10, "network_tx_bytes": 20})
    monkeypatch.setattr("apps.players.service.online_players", lambda inst: {"online": 2, "max": 20,
                                                                            "players": ["Steve", "Alex"]})
    assert sample_all() == 1
    s = MetricSample.objects.get(instance=instance)
    assert (s.cpu_percent, s.players_online, s.players_max) == (12.5, 2, 20)
    assert set(PlayerSeen.objects.values_list("name", flat=True)) == {"Steve", "Alex"}


def test_record_players_sessions(instance):
    t0 = timezone.now()
    record_players(instance, ["Steve"], t0)
    record_players(instance, ["Steve"], t0 + timedelta(minutes=1))
    record_players(instance, ["Steve"], t0 + timedelta(hours=2))  # gap => new session
    seen = PlayerSeen.objects.get(instance=instance, name="Steve")
    assert seen.minutes_online == 3 and seen.sessions == 2


def test_metrics_view_downsamples(instance):
    owner = User.objects.create_user("owner", password="x" * 12, is_owner=True)
    now = timezone.now()
    for i in range(30):
        MetricSample.objects.create(instance=instance, taken_at=now - timedelta(minutes=i), cpu_percent=float(i),
                                    memory_bytes=100, memory_limit_bytes=200, network_rx_bytes=1000 + i,
                                    network_tx_bytes=0, players_online=i % 3)
    client = APIClient()
    client.force_login(owner)
    r = client.get(f"/api/instances/{instance.id}/metrics", {"range": "6h"})
    assert r.status_code == 200
    pts = r.json()["points"]
    assert r.json()["bucket_minutes"] == 5 and 6 <= len(pts) <= 7
    assert all(p["players_online"] is not None for p in pts)
    assert client.get(f"/api/instances/{instance.id}/metrics", {"range": "1y"}).status_code == 400
