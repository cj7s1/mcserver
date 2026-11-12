"""instances.services: host port bookkeeping, conflict checks and allocation."""

from __future__ import annotations

import pytest

from apps.core.errors import ApiError, ConflictError
from apps.core.models import SystemSetting
from apps.instances import services
from apps.nodes.client import AgentUnavailable
from apps.nodes.models import Node

pytestmark = pytest.mark.django_db


def test_ports_in_use_lists_instances_extra_ports_and_docker(fake_agent, node, make_instance):
    a = make_instance(
        name="Alpha",
        game_port=25600,
        extra_ports=[{"container_port": 24454, "host_port": 24454, "protocol": "udp", "label": "voice"}],
    )
    make_instance(name="Beta", game_port=25601, extra_ports=[{"bogus": True}])
    fake_agent.add_foreign_port(25602, container="plex")
    used = services.ports_in_use(node)
    assert used[25600] == "server 'Alpha'"
    assert used[24454] == "server 'Alpha' (voice)"
    assert used[25601] == "server 'Beta'"
    assert used[25602] == "Docker container 'plex'"
    # excluding an instance frees its own ports (including its published container ports)
    fake_agent.apply(
        a.id, {"image": "itzg/minecraft-server:java17", "ports": [{"container_port": 25565, "host_port": 25600}]}, start=True
    )
    used = services.ports_in_use(node, exclude=a)
    assert 25600 not in used and 24454 not in used
    assert 25601 in used and 25602 in used


def test_other_nodes_do_not_conflict(fake_agent, node, make_instance):
    other = Node.objects.create(name="other", agent_url="http://other.invalid:8700")
    make_instance(node=other, game_port=25600)
    assert 25600 not in services.ports_in_use(node)
    services.check_port_available(node, 25600)


@pytest.mark.parametrize("port", [0, 80, 1023, 65536])
def test_check_port_range(fake_agent, node, port):
    with pytest.raises(ApiError) as exc:
        services.check_port_available(node, port)
    assert exc.value.error_code == "invalid_port"
    assert exc.value.status_code == 400


def test_conflict_with_instance(fake_agent, node, make_instance):
    a = make_instance(name="Alpha", game_port=25610)
    with pytest.raises(ConflictError) as exc:
        services.check_port_available(node, 25610)
    assert exc.value.error_code == "port_conflict"
    assert exc.value.status_code == 409
    assert exc.value.details == {"port": 25610}
    assert "Alpha" in str(exc.value.detail)
    services.check_port_available(node, 25610, exclude=a)  # its own port is fine


def test_conflict_with_foreign_container(fake_agent, node):
    fake_agent.add_foreign_port(25620, container="velocity")
    with pytest.raises(ConflictError) as exc:
        services.check_port_available(node, 25620)
    assert "velocity" in str(exc.value.detail)


def test_allocate_first_free_port_in_range(fake_agent, node, make_instance, settings):
    settings.MCPANEL_PORT_RANGE_START = 25565
    settings.MCPANEL_PORT_RANGE_END = 25570
    make_instance(game_port=25565)
    fake_agent.add_foreign_port(25566)
    make_instance(game_port=25568)
    assert services.allocate_port(node) == 25567


def test_allocate_uses_system_setting_range(fake_agent, node, settings):
    settings.MCPANEL_PORT_RANGE_START = 25565
    settings.MCPANEL_PORT_RANGE_END = 25665
    SystemSetting.set("port_range_start", 30000)
    SystemSetting.set("port_range_end", 30010)
    assert services.port_range() == (30000, 30010)
    assert services.allocate_port(node) == 30000


def test_allocate_exhausted_range(fake_agent, node, make_instance, settings):
    settings.MCPANEL_PORT_RANGE_START = 25565
    settings.MCPANEL_PORT_RANGE_END = 25566
    make_instance(game_port=25565)
    make_instance(game_port=25566)
    with pytest.raises(ConflictError) as exc:
        services.allocate_port(node)
    assert exc.value.error_code == "no_free_ports"


def test_agent_unreachable_still_checks_panel_ports(fake_agent, node, make_instance):
    fake_agent.failures["published_ports"] = AgentUnavailable("down")
    make_instance(game_port=25630)
    used = services.ports_in_use(node)
    assert set(used) == {25630}
    with pytest.raises(ConflictError):
        services.check_port_available(node, 25630)


@pytest.mark.django_db
def _stranger(make_instance, make_user, roles):
    """Sees one other server (review F-11: an account that sees no server at all gets 403 here)."""
    from apps.accounts.models import InstanceAccess

    user = make_user("stranger")
    InstanceAccess.objects.create(user=user, instance=make_instance(name="Theirs", game_port=25599), role=roles["viewer"])
    return user


def test_ports_endpoint_hides_names_of_invisible_servers(make_instance, fake_agent, client_for, users, make_user, roles):
    make_instance(name="Secret Server", game_port=25570)
    owner = client_for(users["owner"]).get("/api/system/ports").json()
    assert any(p["used_by"] == "server 'Secret Server'" for p in owner["in_use"])
    assert client_for(users["norole"]).get("/api/system/ports").status_code == 403
    stranger = client_for(_stranger(make_instance, make_user, roles)).get("/api/system/ports").json()
    assert all("Secret Server" not in p["used_by"] for p in stranger["in_use"])
    assert any(p["port"] == 25570 and p["used_by"] == "another server" for p in stranger["in_use"])


@pytest.mark.django_db
def test_ports_endpoint_reports_the_requested_node(make_instance, fake_agent, client_for, users, make_user, roles):
    other = Node.objects.create(name="node-2", agent_url="http://agent-2.invalid:8700", agent_token="t" * 30)
    make_instance(name="Here", game_port=25571)
    make_instance(name="There", game_port=25572, node=other)
    c = client_for(users["owner"])
    default = {p["port"] for p in c.get("/api/system/ports").json()["in_use"]}
    remote = c.get(f"/api/system/ports?node={other.pk}").json()
    assert 25571 in default and 25572 not in default
    assert {p["port"] for p in remote["in_use"]} >= {25572} and 25571 not in {p["port"] for p in remote["in_use"]}
    # visibility rules are the same on every node
    stranger = client_for(_stranger(make_instance, make_user, roles)).get(f"/api/system/ports?node={other.pk}").json()
    assert any(p["port"] == 25572 and p["used_by"] == "another server" for p in stranger["in_use"])
    for bad in ("999999", "abc", "1;2"):
        resp = c.get(f"/api/system/ports?node={bad}")
        assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found", bad
