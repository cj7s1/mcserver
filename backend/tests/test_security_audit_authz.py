"""Regression tests for the authorization / account findings of the security audit.

Each section names the finding it covers. The WebSocket tests use transactional databases
because the consumers reach the database from other threads.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

import pytest
import pytest_asyncio
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.accounts.models import InstanceAccess, PasswordResetToken, Role, User
from apps.accounts.permissions import ALL_CODES
from apps.backups.models import Backup, BackupStatus
from apps.instances.models import Instance, Schedule
from apps.jobs.models import Job, JobStatus
from apps.nodes.models import Node

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db


def err(resp) -> str:
    return resp.json()["error"]["code"]


def custom_role(key: str, *codes: str) -> Role:
    return Role.objects.create(key=key, name=key.title(), permissions=sorted(codes))


# --- 1. server.properties never returns the RCON password ---------------------------------------
PROPS = "#Minecraft server properties\nmotd=hello\nenable-rcon=true\nrcon.port=25575\nrcon.password=s3cr3t-rcon\n"


def test_properties_redact_the_rcon_password(clients, make_instance, fake_agent):
    inst = make_instance()
    fake_agent.put_file(inst.id, "server.properties", PROPS)
    for who in ("viewer", "manager", "owner"):
        resp = clients[who].get(f"/api/instances/{inst.id}/properties")
        assert resp.status_code == 200
        assert "s3cr3t-rcon" not in resp.content.decode(), who
        body = resp.json()
        assert body["values"]["rcon.password"] == "********" and body["redacted_keys"] == ["rcon.password"]
        assert body["values"]["motd"] == "hello"

    m = clients["manager"]
    raw = m.get(f"/api/instances/{inst.id}/properties").json()["raw"]
    # saving the raw text as shown (masked secret + another edit) keeps the real password
    saved = m.patch(f"/api/instances/{inst.id}/properties", {"raw": raw.replace("motd=hello", "motd=bye")}, format="json")
    assert saved.status_code == 200, saved.content
    assert "s3cr3t-rcon" not in saved.content.decode()
    assert saved.json()["changed"] == ["motd"]
    text = fake_agent.text(inst.id, "server.properties")
    assert "rcon.password=s3cr3t-rcon" in text and "motd=bye" in text
    # an actual change of the managed key is still refused
    changed = m.patch(
        f"/api/instances/{inst.id}/properties", {"raw": raw.replace("rcon.password=********", "rcon.password=x")}, format="json"
    )
    assert changed.status_code == 400
    # the form editor sending the masked value back is not a change either
    form = m.patch(f"/api/instances/{inst.id}/properties", {"values": {"rcon.password": "********", "pvp": False}}, format="json")
    assert form.status_code == 200 and form.json()["changed"] == ["pvp"]
    assert "rcon.password=s3cr3t-rcon" in fake_agent.text(inst.id, "server.properties")


# --- 2. custom env values only for instance.configure -------------------------------------------------
def test_env_values_hidden_without_configure(clients, make_instance):
    inst = make_instance(env={"DISCORD_TOKEN": "tok-123", "WHITELIST": "a,b"})
    viewer = clients["viewer"]
    detail = viewer.get(f"/api/instances/{inst.id}").json()
    assert detail["env"] == {"DISCORD_TOKEN": "********", "WHITELIST": "********"}
    assert detail["env_values_hidden"] is True
    listed = next(i for i in viewer.get("/api/instances/").json() if i["id"] == str(inst.id))
    assert listed["env"] == detail["env"] and "tok-123" not in json.dumps(listed)
    for who in ("manager", "owner"):
        body = clients[who].get(f"/api/instances/{inst.id}").json()
        assert body["env"] == {"DISCORD_TOKEN": "tok-123", "WHITELIST": "a,b"} and body["env_values_hidden"] is False


# --- 3 / 15. WebSockets re-check access; RCON runs off the shared sync thread ---------------------------
def _ws_app():
    from channels.auth import AuthMiddlewareStack
    from channels.routing import URLRouter

    from apps.console.routing import websocket_urlpatterns

    return AuthMiddlewareStack(URLRouter(websocket_urlpatterns))


def _session_cookie(user: Any) -> str:
    from django.conf import settings

    client = APIClient()
    client.force_login(user)
    return client.cookies[settings.SESSION_COOKIE_NAME].value


class WsClient:
    """Minimal WebSocket test client (channels.testing needs daphne, which is not installed)."""

    def __init__(self, path: str, session_key: str) -> None:
        from asgiref.testing import ApplicationCommunicator
        from django.conf import settings

        cookie = f"{settings.SESSION_COOKIE_NAME}={session_key}".encode()
        scope = {"type": "websocket", "path": path, "query_string": b"", "headers": [(b"cookie", cookie)], "subprotocols": []}
        self.comm = ApplicationCommunicator(_ws_app(), scope)
        self.session_key = session_key

    async def connect(self, *, auth: bool = True) -> tuple[bool, Any]:
        """Open the socket and, like the frontend, send the session's auth frame first (D-1; ``auth=False``:
        the test sends its own)."""
        from tests.conftest import SESSION_TOKENS

        await self.comm.send_input({"type": "websocket.connect"})
        out = await self.comm.receive_output(2)
        token = SESSION_TOKENS.get(self.session_key)
        if out["type"] == "websocket.accept" and auth and token:
            await self.send_json_to({"type": "auth", "token": token})
            ok = await self.receive_json_from(timeout=3)  # the consumer's first frame (D-1: before anything else)
            assert ok == {"type": "auth_ok"}, ok
            await asyncio.sleep(0.05)  # (the events consumer joins its group right after)
        return out["type"] == "websocket.accept", out

    async def send_json_to(self, data: dict[str, Any]) -> None:
        await self.comm.send_input({"type": "websocket.receive", "text": json.dumps(data)})

    async def receive_output(self, timeout: float = 2) -> dict[str, Any]:
        return await self.comm.receive_output(timeout)

    async def receive_json_from(self, timeout: float = 2) -> dict[str, Any]:
        out = await self.comm.receive_output(timeout)
        assert out["type"] == "websocket.send", out
        return json.loads(out["text"])

    async def receive_nothing(self, timeout: float = 0.1) -> bool:
        return await self.comm.receive_nothing(timeout)

    async def disconnect(self) -> None:
        await self.comm.send_input({"type": "websocket.disconnect", "code": 1000})
        with contextlib.suppress(Exception):
            await self.comm.wait(2)


def _communicator(path: str, session_key: str) -> WsClient:
    return WsClient(path, session_key)


@pytest_asyncio.fixture
async def ws_env(monkeypatch):  # type: ignore[no-untyped-def]
    """Fast re-checks, no real agent log relay, recorded RCON commands."""
    from channels.db import database_sync_to_async
    from django.db import connections

    from apps.console import consumers

    monkeypatch.setattr(consumers, "REVALIDATE_SECONDS", 0.05)

    async def idle_relay(self):  # type: ignore[no-untyped-def]
        await asyncio.Event().wait()

    monkeypatch.setattr(consumers.ConsoleConsumer, "_relay", idle_relay)
    sent: list[str] = []

    def agent_command(node, instance_id, command):  # type: ignore[no-untyped-def]
        sent.append(command)
        return "ok"

    monkeypatch.setattr(consumers, "_agent_command", agent_command)
    yield sent
    # the consumers' database work ran in the shared sync thread: close its connection so the
    # test database can be dropped at the end of the session
    await database_sync_to_async(connections.close_all)()


async def _db(fn, *args):  # type: ignore[no-untyped-def]
    from channels.db import database_sync_to_async

    return await database_sync_to_async(fn)(*args)


def _console_setup(role_key: str = "operator") -> tuple[Any, Any, str]:
    from apps.accounts.views import ensure_system_roles

    ensure_system_roles()
    node = Node.objects.create(name="ws-node", agent_url="http://agent.invalid:8700", is_default=True)
    inst = Instance.objects.create(name="WS", node=node, game_port=41999, image="itzg/minecraft-server:stable-java21")
    user = User.objects.create_user("wsuser", password=PASSWORD, role=Role.objects.get(key=role_key))
    return inst, user, _session_cookie(user)


async def _closed_with(comm, code: int) -> None:  # type: ignore[no-untyped-def]
    for _ in range(50):
        out = await comm.receive_output(timeout=2)
        if out["type"] == "websocket.close":
            assert out.get("code") == code, out
            return
    raise AssertionError("socket was not closed")


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_console_command_rechecks_permissions(ws_env):
    inst, user, key = await _db(_console_setup)
    comm = _communicator(f"/ws/instances/{inst.id}/console/", key)
    connected, _ = await comm.connect()
    assert connected
    assert await comm.receive_json_from() == {"type": "hello", "can_command": True}
    await comm.send_json_to({"type": "command", "id": 1, "command": "say hi"})
    assert (await comm.receive_json_from())["ok"] is True
    # downgraded to viewer: the next command is refused and the client is told
    await _db(lambda: User.objects.filter(pk=user.pk).update(role=Role.objects.get(key="viewer")))
    await comm.send_json_to({"type": "command", "id": 2, "command": "op mallory"})
    assert await comm.receive_json_from() == {"type": "hello", "can_command": False}
    refused = await comm.receive_json_from()
    assert refused["id"] == 2 and refused["ok"] is False
    # all access removed: the socket is closed, nothing reaches RCON
    await _db(lambda: User.objects.filter(pk=user.pk).update(role=None))
    await comm.send_json_to({"type": "command", "id": 3, "command": "stop"})
    await _closed_with(comm, 4403)
    assert ws_env == ["say hi"]
    await comm.disconnect()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["disable", "delete", "logout", "password"])
async def test_console_socket_closes_when_the_account_or_session_ends(ws_env, change):
    from django.contrib.sessions.models import Session

    inst, user, key = await _db(_console_setup)
    comm = _communicator(f"/ws/instances/{inst.id}/console/", key)
    assert (await comm.connect())[0]
    await comm.receive_json_from()  # hello

    def apply() -> None:
        if change == "disable":
            User.objects.filter(pk=user.pk).update(is_active=False)
        elif change == "delete":
            User.objects.filter(pk=user.pk).delete()
        elif change == "logout":
            Session.objects.filter(session_key=key).delete()
        else:
            u = User.objects.get(pk=user.pk)
            u.set_password("A-completely-new-passphrase-1")
            u.save()

    await _db(apply)
    await _closed_with(comm, 4401)  # periodic re-check, no command needed
    assert ws_env == []
    await comm.disconnect()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_events_socket_closes_when_the_user_is_disabled(ws_env):
    _inst, user, key = await _db(_console_setup)
    comm = _communicator("/ws/events/", key)
    assert (await comm.connect())[0]
    await _db(lambda: User.objects.filter(pk=user.pk).update(is_active=False))
    await _closed_with(comm, 4401)
    await comm.disconnect()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_events_socket_uses_fresh_permissions(ws_env):
    from channels.layers import get_channel_layer

    from apps.console.events import EVENTS_GROUP

    inst, user, key = await _db(_console_setup, "viewer")
    comm = _communicator("/ws/events/", key)
    assert (await comm.connect())[0]
    layer = get_channel_layer()
    event = {"type": "panel.event", "event": "instance.status", "payload": {}, "instance_id": str(inst.id)}
    await layer.group_send(EVENTS_GROUP, event)
    assert (await comm.receive_json_from())["instance_id"] == str(inst.id)
    await _db(lambda: User.objects.filter(pk=user.pk).update(role=None))
    await asyncio.sleep(0.3)  # several re-check periods
    await layer.group_send(EVENTS_GROUP, event)
    assert await comm.receive_nothing(timeout=0.3)
    await comm.disconnect()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_rcon_command_does_not_block_the_shared_sync_thread(ws_env, monkeypatch):
    from channels.db import database_sync_to_async

    from apps.console import consumers

    def slow_command(node, instance_id, command):  # type: ignore[no-untyped-def]
        time.sleep(1.5)
        return "late"

    monkeypatch.setattr(consumers, "_agent_command", slow_command)
    inst, _user, key = await _db(_console_setup)
    comm = _communicator(f"/ws/instances/{inst.id}/console/", key)
    assert (await comm.connect())[0]
    await comm.receive_json_from()
    await comm.send_json_to({"type": "command", "id": 1, "command": "list"})
    await asyncio.sleep(0.3)  # the command is now waiting on the (slow) agent
    started = time.monotonic()
    assert await asyncio.wait_for(database_sync_to_async(lambda: "free")(), timeout=1.0) == "free"
    assert time.monotonic() - started < 0.8
    result = await comm.receive_json_from(timeout=5)
    assert result["ok"] is True and result["output"] == "late"
    await comm.disconnect()


# --- 4. schedules need the permission of their action ----------------------------------------------
@pytest.fixture
def scheduler(make_user, client_for):
    role = custom_role("scheduler", "instance.view", "instance.schedules")
    return client_for(make_user("sched", role=role)), role


def test_schedules_require_the_action_permission(scheduler, make_instance, clients):
    client, role = scheduler
    inst = make_instance()
    url = f"/api/instances/{inst.id}/schedules"
    for action, payload in [
        ("command", {"command": "op mallory"}),
        ("restart", {}),
        ("start", {}),
        ("stop", {}),
        ("backup", {}),
        ("snapshot", {}),
    ]:
        resp = client.post(url, {"name": "x", "action": action, "cron": "0 4 * * *", "payload": payload}, format="json")
        assert resp.status_code == 403, action
    assert not Schedule.objects.exists()

    role.permissions = sorted([*role.permissions, "instance.control"])
    role.save()
    ok = client.post(url, {"name": "nightly", "action": "restart", "cron": "0 4 * * *"}, format="json")
    assert ok.status_code == 201
    sid = ok.json()["id"]
    # turning an allowed schedule into a command needs instance.command
    to_cmd = client.patch(f"{url}/{sid}", {"action": "command", "payload": {"command": "op mallory"}}, format="json")
    assert to_cmd.status_code == 403
    assert Schedule.objects.get(pk=sid).action == "restart"
    assert client.patch(f"{url}/{sid}", {"cron": "0 5 * * *"}, format="json").status_code == 200

    # a command schedule someone else made: may be switched off or deleted, not edited
    cmd = clients["owner"].post(
        url, {"name": "say", "action": "command", "cron": "0 4 * * *", "payload": {"command": "say hi"}}, format="json"
    )
    cid = cmd.json()["id"]
    assert client.patch(f"{url}/{cid}", {"payload": {"command": "op mallory"}}, format="json").status_code == 403
    assert client.patch(f"{url}/{cid}", {"cron": "* * * * *"}, format="json").status_code == 403
    assert client.patch(f"{url}/{cid}", {"enabled": True}, format="json").status_code == 403
    assert client.patch(f"{url}/{cid}", {"enabled": False}, format="json").status_code == 200
    assert Schedule.objects.get(pk=cid).payload == {"command": "say hi"}
    assert client.delete(f"{url}/{cid}").status_code == 204


def test_snapshot_retention_in_a_schedule_needs_restore(make_user, client_for, make_instance):
    role = custom_role("snapper", "instance.view", "instance.schedules", "instance.backup")
    client = client_for(make_user("snap", role=role))
    url = f"/api/instances/{make_instance().id}/schedules"
    body = {"name": "s", "action": "snapshot", "cron": "0 * * * *"}
    # every snapshot schedule prunes older snapshots (default keep 24), so all need instance.restore
    assert client.post(url, body, format="json").status_code == 403
    assert client.post(url, {**body, "payload": {"keep": 1}}, format="json").status_code == 403
    restorer = client_for(
        make_user(
            "snap2", role=custom_role("snapper2", "instance.view", "instance.schedules", "instance.backup", "instance.restore")
        )
    )
    assert restorer.post(url, body, format="json").status_code == 201


# --- 5. node tokens cannot be redirected ---------------------------------------------------------------
TOKEN = "t" * 32


@pytest.fixture
def node_admin(make_user, client_for):
    return client_for(make_user("nodeadmin", role=custom_role("nodes", "system.nodes")))


def test_node_url_change_needs_the_token_again(node_admin, node):
    remote = Node.objects.create(name="n2", agent_url="http://10.0.0.2:8700", agent_token=TOKEN, use_settings_token=False)
    resp = node_admin.patch(f"/api/nodes/{remote.id}", {"agent_url": "http://evil.example:8700"}, format="json")
    assert resp.status_code == 400 and "agent_token" in resp.json()["error"]["details"]["fields"]
    remote.refresh_from_db()
    assert remote.agent_url == "http://10.0.0.2:8700"
    ok = node_admin.patch(
        f"/api/nodes/{remote.id}", {"agent_url": "http://10.0.0.3:8700", "agent_token": "n" * 32}, format="json"
    )
    assert ok.status_code == 200
    remote.refresh_from_db()
    assert remote.agent_url == "http://10.0.0.3:8700" and remote.agent_token == "n" * 32
    # other fields stay editable without the token
    assert node_admin.patch(f"/api/nodes/{remote.id}", {"lan_address": "10.0.0.3"}, format="json").status_code == 200


def test_settings_token_is_not_user_writable(node_admin, node):
    resp = node_admin.post("/api/nodes", {"name": "x", "agent_url": "http://evil.example:8700", "use_settings_token": True})
    assert resp.status_code == 400  # needs its own token: use_settings_token is ignored
    assert not Node.objects.filter(name="x").exists()
    made = node_admin.post(
        "/api/nodes", {"name": "y", "agent_url": "http://10.0.0.9:8700", "use_settings_token": True, "agent_token": TOKEN}
    )
    assert made.status_code == 201
    assert Node.objects.get(name="y").use_settings_token is False


def test_default_node_cannot_be_repointed(node_admin, node):
    assert node.is_default and node.use_settings_token
    for body in (
        {"agent_url": "http://evil.example:8700"},
        {"agent_token": TOKEN},
        {"tls_fingerprint": "ab" * 32},
        {"name": "renamed"},
    ):
        assert node_admin.patch(f"/api/nodes/{node.id}", body, format="json").status_code == 400, body
    node.refresh_from_db()
    assert node.agent_url == "http://agent.invalid:8700" and node.use_settings_token
    assert node_admin.patch(f"/api/nodes/{node.id}", {"lan_address": "192.168.1.5"}, format="json").status_code == 200
    # sending the unchanged values back (a full form) is fine
    same = node_admin.patch(f"/api/nodes/{node.id}", {"agent_url": node.agent_url, "name": node.name}, format="json")
    assert same.status_code == 200


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.2:8700#",
        "http://10.0.0.2:8700#/v1",
        "http://good.lan@evil.example:8700",
        "http://user:pw@10.0.0.2:8700",
        "http://10.0.0.2:8700/;x",
        "http://10.0.0.2:99999",
        "http://10.0.0.2:8700/?a=b",
    ],
)
def test_agent_url_rejects_fragments_userinfo_and_junk(node_admin, node, url):
    resp = node_admin.post("/api/nodes", {"name": "n3", "agent_url": url, "agent_token": TOKEN}, format="json")
    assert resp.status_code == 400, url


# --- 6. no Administrator-equivalent roles from non-owners; role edits respect rank -----------------------
def test_admin_cannot_mint_administrator_equivalents(clients, users, roles):
    admin = clients["admin"]
    for codes in (sorted(ALL_CODES), ["system.users"], ["system.settings", "instance.view"]):
        resp = admin.post("/api/roles", {"key": "k" + str(len(codes)), "name": "N", "permissions": codes}, format="json")
        assert resp.status_code == 403, codes
    ok = admin.post("/api/roles", {"key": "ops", "name": "Ops", "permissions": ["system.audit", "instance.view"]}, format="json")
    assert ok.status_code == 201
    # a role carrying administration rights, made by the owner, cannot be handed out by an admin
    owner_role = clients["owner"].post(
        "/api/roles", {"key": "usermgr", "name": "User manager", "permissions": ["system.users", "instance.view"]}, format="json"
    )
    assert owner_role.status_code == 201
    rid = owner_role.json()["id"]
    assert admin.post("/api/users", {"username": "um", "password": "Fresh-passphrase-77", "role": rid}).status_code == 403
    assert admin.patch(f"/api/users/{users['viewer'].id}", {"role": rid}, format="json").status_code == 403
    assert clients["owner"].patch(f"/api/users/{users['viewer'].id}", {"role": rid}, format="json").status_code == 200
    # admins cannot widen it either
    widened = admin.patch(f"/api/roles/{rid}", {"permissions": ["system.users", "system.settings"]}, format="json")
    assert widened.status_code == 403


def test_role_edit_respects_out_ranking_holders(make_user, client_for, make_instance, roles):
    editor = client_for(make_user("ed", role=custom_role("editor", "system.users", "instance.view", "instance.control")))
    team = custom_role("team", "instance.view")
    holder = make_user("holder", role=team)
    # the holder also has server access the editor lacks: the editor out-ranks nobody here
    InstanceAccess.objects.create(user=holder, instance=make_instance(), role=roles["server_manager"])
    resp = editor.patch(f"/api/roles/{team.id}", {"permissions": ["instance.view", "instance.control"]}, format="json")
    assert resp.status_code == 403
    team.refresh_from_db()
    assert team.permissions == ["instance.view"]
    # a role with permissions the editor lacks cannot be changed (not even reduced)
    big = custom_role("big", "instance.view", "instance.delete")
    assert editor.patch(f"/api/roles/{big.id}", {"permissions": ["instance.view"]}, format="json").status_code == 403
    # holders the editor out-ranks: fine
    small = custom_role("small", "instance.view")
    make_user("low", role=small)
    assert (
        editor.patch(f"/api/roles/{small.id}", {"permissions": ["instance.view", "instance.control"]}, format="json").status_code
        == 200
    )


def test_admins_cannot_take_over_superusers(clients, make_user):
    root = make_user("root3")
    User.objects.filter(pk=root.pk).update(is_superuser=True)
    assert clients["admin"].post(f"/api/users/{root.id}/reset-link").status_code == 403
    assert clients["owner"].post(f"/api/users/{root.id}/reset-link").status_code == 201


# --- 7. optional first-run setup token ---------------------------------------------------------------
def test_setup_token_is_required_when_configured(roles, settings):
    settings.MCPANEL_SETUP_TOKEN = "setup-token-1234567890"
    client = APIClient()
    assert client.get("/api/auth/state").json()["setup_token_required"] is True
    body = {"username": "first", "password": "a-Very-long-passphrase"}
    missing = client.post("/api/auth/setup", body)
    assert missing.status_code == 403 and err(missing) == "setup_token_invalid"
    wrong = client.post("/api/auth/setup", {**body, "setup_token": "nope"})
    assert wrong.status_code == 403
    assert not User.objects.exists()
    ok = client.post("/api/auth/setup", {**body, "setup_token": "setup-token-1234567890"})
    assert ok.status_code == 201 and ok.json()["is_owner"] is True


def test_setup_token_from_the_environment(roles, monkeypatch, settings):
    if hasattr(settings, "MCPANEL_SETUP_TOKEN"):
        del settings.MCPANEL_SETUP_TOKEN
    monkeypatch.setenv("MCPANEL_SETUP_TOKEN", "env-token-1234567890")
    body = {"username": "first", "password": "a-Very-long-passphrase"}
    assert APIClient().post("/api/auth/setup", body).status_code == 403
    assert APIClient().post("/api/auth/setup", {**body, "setup_token": "env-token-1234567890"}).status_code == 201


def test_open_setup_is_logged_once(roles, settings, monkeypatch, caplog):
    from apps.accounts import views

    settings.MCPANEL_SETUP_TOKEN = ""
    monkeypatch.setattr(views, "_open_setup_warned", False)
    with caplog.at_level(logging.WARNING, logger="apps.accounts.views"):
        for _ in range(3):
            state = APIClient().get("/api/auth/state").json()
    assert "setup_token_required" not in state
    warnings = [r for r in caplog.records if "setup is open" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING


# --- 8. reset tokens: never in a URL sent to the server, single-use, die with any password change ----------
def test_reset_link_keeps_the_token_out_of_the_request_url(clients, users):
    link = clients["owner"].post(f"/api/users/{users['viewer'].id}/reset-link").json()
    assert link["path"] == f"/reset-password#token={link['token']}" and "?" not in link["path"]
    # the reset endpoint only takes the token from the POST body
    assert APIClient().get(f"/api/auth/reset?token={link['token']}").status_code == 405


def test_password_change_invalidates_outstanding_reset_tokens(clients, users, client_for):
    raw = clients["owner"].post(f"/api/users/{users['viewer'].id}/reset-link").json()["token"]
    me = client_for(users["viewer"])
    assert me.post("/api/auth/password", {"old_password": PASSWORD, "new_password": "Brand-new-passphrase-42"}).status_code == 204
    resp = APIClient().post("/api/auth/reset", {"token": raw, "new_password": "Attacker-passphrase-99"})
    assert resp.status_code == 400 and err(resp) == "invalid_token"
    users["viewer"].refresh_from_db()
    assert users["viewer"].check_password("Brand-new-passphrase-42")


def test_reset_tokens_expire_and_unbound_tokens_are_void(users):
    from datetime import timedelta

    from django.utils import timezone

    token, raw = PasswordResetToken.issue(users["viewer"])
    PasswordResetToken.objects.filter(pk=token.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert APIClient().post("/api/auth/reset", {"token": raw, "new_password": "Reset-passphrase-99"}).status_code == 400
    legacy, raw2 = PasswordResetToken.issue(users["viewer"])
    PasswordResetToken.objects.filter(pk=legacy.pk).update(password_fingerprint="")
    assert APIClient().post("/api/auth/reset", {"token": raw2, "new_password": "Reset-passphrase-99"}).status_code == 400


# --- 9. must_change_password is enforced by the API ------------------------------------------------------
def test_must_change_password_blocks_the_api(make_user, client_for, make_instance, fake_agent):
    inst = make_instance()
    user = make_user("fresh", role="administrator", must_change_password=True)
    c = client_for(user)
    for url in (
        "/api/instances/",
        f"/api/instances/{inst.id}",
        f"/api/instances/{inst.id}/properties",
        "/api/jobs",
        "/api/users",
        "/api/roles",
        "/api/nodes",
        "/api/permissions",
        "/api/system/settings",
        "/api/audit",
        # views without their own permission helper: covered by DEFAULT_PERMISSION_CLASSES
        "/api/system/info",
        "/api/catalog/server-types",
        "/api/modpacks/providers",
        "/api/notifications/events",
    ):
        resp = c.get(url)
        assert resp.status_code == 403 and err(resp) == "must_change_password", url
    assert c.post(f"/api/instances/{inst.id}/power", {"action": "start"}, format="json").status_code == 403
    assert c.get("/api/auth/me").status_code == 200
    assert c.get("/api/auth/state").json()["user"]["must_change_password"] is True
    assert c.get("/api/schema").status_code == 403  # SERVE_PERMISSIONS
    assert c.get("/api/health").status_code in (200, 503)
    assert c.post("/api/auth/password", {"old_password": PASSWORD, "new_password": "Brand-new-passphrase-42"}).status_code == 204
    assert c.get("/api/instances/").status_code == 200


# --- 10. abandoning jobs needs the job's own permission; revoked access hides jobs ----------------------
def _running_job(inst: Instance, kind: str, user: Any = None) -> Job:
    job = Job.objects.create(kind=kind, title=kind, instance=inst, user=user, status=JobStatus.RUNNING, exclusive=True)
    Instance.objects.filter(pk=inst.pk).update(active_job=job)
    return job


@pytest.mark.parametrize(("kind", "allowed"), [("restore", False), ("delete", False), ("migrate", False), ("restart", True)])
def test_abandon_needs_the_permission_of_the_job_kind(clients, make_instance, kind, allowed):
    job = _running_job(make_instance(), kind)
    resp = clients["operator"].post(f"/api/jobs/{job.id}/abandon")
    assert resp.status_code == (200 if allowed else 403), resp.content
    job.refresh_from_db()
    assert (job.status == JobStatus.FAILED) is allowed
    if not allowed:
        assert clients["admin"].post(f"/api/jobs/{job.id}/abandon").status_code == 200  # system.settings


def test_jobs_disappear_when_server_access_is_revoked(make_user, client_for, make_instance, roles):
    inst = make_instance()
    user = make_user("granted")
    grant = InstanceAccess.objects.create(user=user, instance=inst, role=roles["operator"])
    job = Job.objects.create(kind="backup", title="mine", instance=inst, user=user, status=JobStatus.SUCCEEDED)
    own = Job.objects.create(kind="backup", title="server-less", user=user, status=JobStatus.SUCCEEDED)
    c = client_for(user)
    assert {j["id"] for j in c.get("/api/jobs").json()} == {str(job.id), str(own.id)}
    grant.delete()
    assert {j["id"] for j in c.get("/api/jobs").json()} == {str(own.id)}
    assert c.get(f"/api/jobs/{job.id}").status_code == 404
    assert c.post(f"/api/jobs/{job.id}/abandon").status_code == 404


# --- 11. node list for users without system.nodes --------------------------------------------------------
def test_node_list_is_minimal_without_system_nodes(clients, node):
    Node.objects.create(name="n2", agent_url="http://10.0.0.2:8700", agent_token=TOKEN, use_settings_token=False)
    plain = clients["norole"].get("/api/nodes")
    assert plain.status_code == 200
    assert all(set(n) == {"id", "name", "is_default"} for n in plain.json())
    full = clients["admin"].get("/api/nodes").json()
    assert {n["agent_url"] for n in full} >= {"http://10.0.0.2:8700"} and "instance_count" in full[0]


# --- 12. archived servers cannot be started through other endpoints -------------------------------------
def test_archived_servers_cannot_be_updated_or_recreated(clients, make_instance, mojang_java):
    from django.utils import timezone

    inst = make_instance(archived_at=timezone.now())
    m = clients["manager"]
    sw = m.post(f"/api/instances/{inst.id}/software", {"minecraft_version": "1.20.4", "start": True}, format="json")
    assert sw.status_code == 409 and err(sw) == "archived"
    rec = m.post(f"/api/instances/{inst.id}/recreate")
    assert rec.status_code == 409 and err(rec) == "archived"
    assert not Job.objects.filter(instance=inst).exists()


def test_archived_modpack_servers_cannot_be_updated_and_started(clients, make_instance):
    from django.utils import timezone

    inst = make_instance(server_type="MODRINTH", modpack_provider="modrinth", modpack_project_id="P1", archived_at=timezone.now())
    resp = clients["manager"].post(f"/api/instances/{inst.id}/modpack/update", {"version_id": "v2", "start": True})
    assert resp.status_code == 409 and err(resp) == "archived"
    assert not Job.objects.filter(instance=inst).exists()


@pytest.mark.parametrize("kind", ["start", "restart", "recreate", "software_update"])
def test_jobs_never_start_an_archived_server(make_instance, fake_agent, django_capture_on_commit_callbacks, kind):
    """apply_container is the central guard, also for jobs queued before the server was archived."""
    from django.utils import timezone

    from apps.jobs.runner import enqueue

    inst = make_instance()
    params = {"start": True}
    if kind == "software_update":
        params.update(minecraft_version="1.20.4", java_version=17, backup=False)
    job = enqueue(kind, title=kind, instance=inst, params=params)  # queued while the server was active
    Instance.objects.filter(pk=inst.pk).update(archived_at=timezone.now())
    from apps.jobs.runner import run_job

    run_job(str(job.id))
    job.refresh_from_db()
    assert (job.status, job.error_code) == (JobStatus.FAILED, "archived"), job.error
    assert not fake_agent.called("apply")


def test_apply_container_rereads_the_archive_flag(make_instance, fake_agent):
    from django.utils import timezone

    from apps.core.errors import ApiError
    from apps.instances.handlers import apply_container

    inst = make_instance()
    stale = Instance.objects.get(pk=inst.pk)  # a copy loaded before the server was archived
    Instance.objects.filter(pk=inst.pk).update(archived_at=timezone.now())
    with pytest.raises(ApiError) as exc:
        apply_container(stale, None, start=True)  # type: ignore[arg-type]
    assert exc.value.error_code == "archived" and exc.value.status_code == 409
    # without start it creates no container either (archiving removed it)
    logged: list[str] = []

    class Ctx:
        def log(self, line: str) -> None:
            logged.append(line)

    assert apply_container(stale, Ctx(), start=False) == {"skipped": "archived"}  # type: ignore[arg-type]
    assert logged and not fake_agent.called("apply")


# --- 13. PATCH: no version change bypass; only edited columns are written ---------------------------------
def test_patch_refuses_version_changes(clients, make_instance):
    inst = make_instance()
    m = clients["manager"]
    for body in ({"minecraft_version": "1.8.9"}, {"loader_version": "0.1"}):
        resp = m.patch(f"/api/instances/{inst.id}", body, format="json")
        assert resp.status_code == 400, body
    # unchanged values in a full form are fine; Java stays editable here
    same = m.patch(f"/api/instances/{inst.id}", {"minecraft_version": "1.20.4", "java_version": 21}, format="json")
    assert same.status_code == 200
    inst.refresh_from_db()
    assert inst.minecraft_version == "1.20.4" and inst.java_version == 21 and inst.image.endswith("java21")


def test_patch_does_not_overwrite_the_job_lock(clients, make_instance, monkeypatch):
    from apps.instances import views

    inst = make_instance()
    job = Job.objects.create(kind="backup", title="b", instance=inst, status=JobStatus.RUNNING, exclusive=True)
    real = views.get_instance_for

    def racing_get_instance_for(user, pk, code="instance.view"):  # type: ignore[no-untyped-def]
        loaded = real(user, pk, code)
        # a job claims the lock and updates status after the request loaded the row
        Instance.objects.filter(pk=pk).update(active_job=job, status="running")
        return loaded

    monkeypatch.setattr(views, "get_instance_for", racing_get_instance_for)
    assert clients["manager"].patch(f"/api/instances/{inst.id}", {"memory_mb": 3072}, format="json").status_code == 200
    inst.refresh_from_db()
    assert inst.memory_mb == 3072 and inst.active_job_id == job.id and inst.status == "running"


# --- 14. the instance list does not query per server -------------------------------------------------------
@pytest.mark.parametrize("who", ["granted", "global_viewer", "owner"])
def test_instance_list_query_count_does_not_grow(make_user, client_for, make_instance, roles, users, who):
    user = make_user("granted") if who == "granted" else users["viewer" if who == "global_viewer" else "owner"]
    c = client_for(user)

    def add(n: int) -> None:
        for _ in range(n):
            inst = make_instance(env={"A": "1"})
            InstanceAccess.objects.create(user=user, instance=inst, role=roles["operator"])

    def count() -> int:
        with CaptureQueriesContext(connection) as ctx:
            resp = c.get("/api/instances/")
        assert resp.status_code == 200
        return len(ctx.captured_queries)

    add(1)
    one = count()
    add(5)
    assert count() == one


# --- 16. restore-as-new / clone leave no orphan when the source is busy ------------------------------------
def test_restore_as_new_and_clone_leave_no_orphan_when_busy(clients, make_instance, fake_agent):
    source = make_instance()
    backup = Backup.objects.create(instance=source, name="b", status=BackupStatus.SUCCEEDED, snapshot_id="ab" * 32)
    _running_job(source, "backup")
    before = set(Instance.objects.values_list("id", flat=True))
    owner = clients["owner"]
    restore = owner.post(f"/api/instances/{source.id}/backups/{backup.id}/restore-as-new", {"name": "copy"}, format="json")
    assert restore.status_code == 409 and err(restore) == "instance_busy"
    clone = owner.post(f"/api/instances/{source.id}/clone", {"name": "copy2"}, format="json")
    assert clone.status_code == 409 and err(clone) == "instance_busy"
    assert set(Instance.objects.values_list("id", flat=True)) == before
