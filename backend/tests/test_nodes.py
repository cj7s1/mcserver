import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.nodes.models import Node

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner_client():
    owner = User.objects.create_user("o", password="x" * 12, is_owner=True)
    c = APIClient()
    c.force_login(owner)
    return c


def test_node_crud_and_validation(owner_client):
    r = owner_client.post(
        "/api/nodes", {"name": "n2", "agent_url": "http://10.0.0.2:8700/", "agent_token": "t" * 30}, format="json"
    )
    assert r.status_code == 201, r.json()
    body = r.json()
    assert "agent_token" not in body and body["agent_url"] == "http://10.0.0.2:8700"
    assert Node.objects.get(name="n2").agent_token == "t" * 30  # stored (encrypted at rest)
    assert (
        owner_client.post(
            "/api/nodes", {"name": "bad", "agent_url": "http://x:1/path", "agent_token": "t" * 30}, format="json"
        ).status_code
        == 400
    )
    assert (
        owner_client.post(
            "/api/nodes", {"name": "short", "agent_url": "http://x:1", "agent_token": "abc"}, format="json"
        ).status_code
        == 400
    )
    assert owner_client.delete(f"/api/nodes/{body['id']}").status_code == 204


def test_non_admin_cannot_manage_nodes():
    u = User.objects.create_user("u", password="x" * 12)
    c = APIClient()
    c.force_login(u)
    assert c.get("/api/nodes").status_code == 200  # names only, for the wizard
    assert (
        c.post("/api/nodes", {"name": "x", "agent_url": "http://x:1", "agent_token": "t" * 30}, format="json").status_code == 403
    )


def test_default_node_cannot_be_deleted(owner_client):
    node = Node.default()
    assert owner_client.delete(f"/api/nodes/{node.id}").status_code == 409
