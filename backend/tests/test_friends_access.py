"""B4 of the feature plan of 2026-11-12: the built-in Friend role and ``POST /api/users/invite``."""

from __future__ import annotations

from typing import Any

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import InstanceAccess, Role, User
from apps.accounts.views import ensure_system_roles
from apps.audit.models import AuditEvent
from apps.instances.models import Instance
from apps.instances.serializers import LIMITED_FIELDS, LIMITED_PLAYER_FIELDS
from apps.jobs.models import Job

from .test_security_audit_authz import ws_env  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db
NEW_PASSWORD = "Friendly-Creeper-Stairs-77"


@pytest.fixture
def friend_setup(make_instance: Any, make_user: Any, roles: dict[str, Any]) -> tuple[Any, Instance, Instance]:
    shared = make_instance(name="Shared")
    other = make_instance(name="Private")
    friend = make_user("robin")
    InstanceAccess.objects.create(user=friend, instance=shared, role=roles["friend"])
    return friend, shared, other


def test_the_friend_role_is_built_in(roles: dict[str, Any]) -> None:
    friend = roles["friend"]
    assert friend.name == "Friend" and friend.is_system
    assert friend.permission_set == frozenset({"instance.control", "instance.overview"})
    # nobody else's permission list changes: "instance.overview" is the Friend's alone
    assert "instance.overview" not in roles["administrator"].permission_set
    assert "instance.overview" not in roles["viewer"].permission_set


def test_a_friend_sees_status_starts_and_stops_and_nothing_else(
    friend_setup: Any, client_for: Any, fake_agent: Any, django_capture_on_commit_callbacks: Any
) -> None:
    friend, shared, other = friend_setup
    client = client_for(friend)
    rows = client.get("/api/instances/").json()
    assert [r["id"] for r in rows] == [str(shared.id)]
    assert tuple(rows[0]) == LIMITED_FIELDS + LIMITED_PLAYER_FIELDS and rows[0]["limited"] is True
    assert rows[0]["players_online"] is None and rows[0]["players_max"] is None  # (not running)
    assert rows[0]["permissions"] == ["instance.control", "instance.overview"]
    detail = client.get(f"/api/instances/{shared.id}").json()
    assert detail["limited"] is True and "env" not in detail and "memory_mb" not in detail and "ports" not in detail
    assert client.get(f"/api/instances/{other.id}").status_code == 404
    share = client.get(f"/api/instances/{shared.id}/share")
    assert share.status_code == 200 and "password" not in share.json() and "password_set" in share.json()
    assert client.get(f"/api/instances/{shared.id}/share", {"with_password": "true"}).status_code == 403
    base = f"/api/instances/{shared.id}"
    for path in (
        "/properties",
        "/config",
        "/files?path=/",
        "/files/text?path=server.properties",
        "/backups",
        "/schedules",
        "/jobs",
        "/players",
        "/metrics",
        "/access",
        "/content",
        "/saves",
    ):
        assert client.get(base + path).status_code == 403, (path, client.get(base + path).status_code)
    assert client.patch(base, {"name": "Mine now"}, format="json").status_code == 403
    assert client.post(f"{base}/command", {"command": "op robin"}, format="json").status_code == 403
    assert client.post(f"{base}/backups", {}, format="json").status_code == 403
    assert client.get("/api/users").status_code == 403 and client.get("/api/audit").status_code == 403
    # start and stop like the web UI's buttons (the same power service, audited as the friend)
    with django_capture_on_commit_callbacks(execute=False):
        started = client.post(f"{base}/power", {"action": "start"}, format="json")
    assert started.status_code in (200, 202), started.json()
    assert Job.objects.get(instance=shared).kind == "start"
    assert AuditEvent.objects.get(action="instance.start").actor_username == "robin"
    # the global job list leaves out the jobs of servers it only sees the status of
    jobs = client.get("/api/jobs")
    assert jobs.status_code == 200 and str(shared.id) not in jobs.content.decode()


def test_a_friend_sees_how_many_play(friend_setup: Any, client_for: Any) -> None:
    from datetime import timedelta

    from django.utils import timezone

    from apps.metrics.models import MetricSample

    friend, shared, _other = friend_setup
    Instance.objects.filter(pk=shared.pk).update(status="running")
    now = timezone.now()
    MetricSample.objects.create(instance=shared, taken_at=now - timedelta(minutes=5), players_online=1, players_max=20)
    MetricSample.objects.create(instance=shared, taken_at=now - timedelta(seconds=30), players_online=2, players_max=20)
    client = client_for(friend)
    detail = client.get(f"/api/instances/{shared.id}").json()
    assert (detail["players_online"], detail["players_max"]) == (2, 20)
    assert client.get("/api/instances/").json()[0]["players_online"] == 2
    # an old sample, or a count the sample could not read: unknown
    MetricSample.objects.filter(players_online=2).update(taken_at=now - timedelta(minutes=4))
    assert client.get(f"/api/instances/{shared.id}").json()["players_online"] is None
    MetricSample.objects.create(instance=shared, taken_at=now, players_online=None, players_max=20)
    detail = client.get(f"/api/instances/{shared.id}").json()
    assert detail["players_online"] is None and detail["players_max"] == 20


def test_viewers_and_owners_keep_the_full_answer(friend_setup: Any, clients: Any) -> None:
    _friend, shared, _other = friend_setup
    for who in ("owner", "viewer", "operator"):
        data = clients[who].get(f"/api/instances/{shared.id}").json()
        assert data["limited"] is False and "env" in data and "instance.overview" not in data["permissions"]
        assert "players_online" not in data  # (the full answer is unchanged)


def test_invite_creates_the_account_and_the_one_time_link(
    make_instance: Any, clients: Any, users: dict[str, Any], roles: dict[str, Any]
) -> None:
    a, b = make_instance(name="Alpha"), make_instance(name="Beta")
    resp = clients["owner"].post(
        "/api/users/invite", {"username": "robin", "email": "", "servers": [str(a.id), str(b.id), str(a.id)]}, format="json"
    )
    assert resp.status_code == 201, resp.json()
    body = resp.json()
    assert body["path"] == f"/reset-password#token={body['token']}" and body["expires_in_hours"] == 24
    assert body["user"]["username"] == "robin" and body["user"]["role"] is None
    assert [s["name"] for s in body["servers"]] == ["Alpha", "Beta"]
    robin = User.objects.get(username="robin")
    assert not robin.has_usable_password() and robin.is_active
    assert {(g.instance_id, g.role.key) for g in InstanceAccess.objects.filter(user=robin)} == {
        (a.id, "friend"),
        (b.id, "friend"),
    }
    invited = AuditEvent.objects.get(action="user.invited")
    assert invited.details == {"role": "friend", "servers": [str(a.id), str(b.id)]} and body["token"] not in str(invited.details)
    assert AuditEvent.objects.filter(action="instance.access_granted", target_id=str(robin.pk)).count() == 2
    # no way in before the friend chose a password; the link sets it once
    anon = APIClient()
    assert anon.post("/api/auth/login", {"username": "robin", "password": ""}, format="json").status_code != 200
    assert anon.post("/api/auth/reset", {"token": body["token"], "new_password": NEW_PASSWORD}, format="json").status_code == 204
    assert anon.post("/api/auth/reset", {"token": body["token"], "new_password": NEW_PASSWORD}, format="json").status_code == 400
    login = APIClient().post("/api/auth/login", {"username": "robin", "password": NEW_PASSWORD}, format="json")
    assert login.status_code == 200, login.json()


def test_who_may_invite(make_instance: Any, clients: Any, make_user: Any, client_for: Any) -> None:
    a = make_instance(name="Alpha")
    payload = {"username": "kim", "servers": [str(a.id)]}
    for who in ("operator", "manager", "viewer", "anon"):
        assert clients[who].post("/api/users/invite", payload, format="json").status_code in (401, 403), who
    # a user manager who may not grant access on that server
    managers = Role.objects.create(key="usermgr", name="User managers", permissions=["system.users"])
    usermgr = make_user("usermgr", role=managers)
    assert client_for(usermgr).post("/api/users/invite", payload, format="json").status_code in (403, 404)
    assert clients["admin"].post("/api/users/invite", payload, format="json").status_code == 201
    assert clients["admin"].post("/api/users/invite", payload, format="json").status_code == 400  # the name is taken
    assert clients["owner"].post("/api/users/invite", {"username": "lee", "servers": []}, format="json").status_code == 400
    unknown = {"username": "lee", "servers": ["00000000-0000-0000-0000-000000000000"]}
    assert clients["owner"].post("/api/users/invite", unknown, format="json").status_code == 404
    assert not User.objects.filter(username__in=("lee",)).exists() and User.objects.filter(username="kim").count() == 1


def test_the_friend_role_through_the_access_page_and_after_a_reset_link(
    friend_setup: Any, clients: Any, users: dict[str, Any], roles: dict[str, Any]
) -> None:
    friend, _shared, other = friend_setup
    resp = clients["admin"].put(
        f"/api/instances/{other.id}/access", {"user": friend.pk, "role": roles["friend"].pk}, format="json"
    )
    assert resp.status_code == 200, resp.json()
    assert clients["admin"].post(f"/api/users/{friend.pk}/reset-link").status_code == 201
    ensure_system_roles()  # (at every start: the role stays as it is)
    assert Role.objects.get(key="friend").permission_set == frozenset({"instance.control", "instance.overview"})


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_friend_gets_status_events_but_not_job_events(ws_env: Any) -> None:  # noqa: F811
    from channels.layers import get_channel_layer

    from apps.console.events import EVENTS_GROUP

    from .test_security_audit_authz import _communicator, _db, _session_cookie

    def setup() -> tuple[str, str]:
        from apps.nodes.models import Node

        ensure_system_roles()
        node = Node.objects.create(name="ws-node", agent_url="http://agent.invalid:8700", is_default=True)
        inst = Instance.objects.create(name="WS", node=node, game_port=41998, image="itzg/minecraft-server:stable-java21")
        friend = User.objects.create_user("wsfriend", password=NEW_PASSWORD)
        InstanceAccess.objects.create(user=friend, instance=inst, role=Role.objects.get(key="friend"))
        return str(inst.id), _session_cookie(friend)

    instance_id, key = await _db(setup)
    socket = _communicator("/ws/events/", key)
    assert (await socket.connect())[0]
    layer = get_channel_layer()

    def event(name: str) -> dict[str, Any]:
        return {"type": "panel.event", "event": name, "payload": {"e": name}, "instance_id": instance_id}

    await layer.group_send(EVENTS_GROUP, event("job.progress"))
    assert await socket.receive_nothing(timeout=0.3)
    await layer.group_send(EVENTS_GROUP, event("instance.status"))
    assert (await socket.receive_json_from(timeout=2))["payload"] == {"e": "instance.status"}
    await socket.disconnect()
