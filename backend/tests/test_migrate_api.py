import pytest

from apps.nodes.models import Node


@pytest.fixture
def other_node(db):
    return Node.objects.create(name="n2", agent_url="http://agent2.invalid:8700", agent_token="t" * 30, use_settings_token=False)


def test_migrate_validation(make_instance, fake_agent, client_for, users, other_node):
    inst = make_instance(name="Mover")
    owner = client_for(users["owner"])
    url = f"/api/instances/{inst.id}/migrate"
    assert owner.post(url, {"node": other_node.id}, format="json").json()["error"]["code"] == "confirmation_required"
    assert owner.post(url, {"node": inst.node_id, "confirm": True}, format="json").status_code == 409
    assert owner.post(url, {"node": 999999, "confirm": True}, format="json").status_code == 400


def test_migrate_needs_node_admin(make_instance, fake_agent, client_for, users, other_node):
    inst = make_instance(name="Mover2")
    # server managers can configure servers but cannot move them between hosts
    r = client_for(users["manager"]).post(
        f"/api/instances/{inst.id}/migrate", {"node": other_node.id, "confirm": True}, format="json"
    )
    assert r.status_code == 403
