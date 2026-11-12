"""D-1 of the security review of 2026-11-11 (interface I-7): the session is bound to the tab that signed in.

The reviewers' proof (d-panel-web/poc test_session_rotation...: "the cookie value used from another address and
user agent is accepted") is turned around here: a cookie alone gets 401 ``session_token_required`` on a sample of
safe, unsafe and download routes and on both WebSockets; the token header, a download ticket or the WebSocket
auth frame are what let a request through.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from django.conf import settings
from rest_framework.test import APIClient

from apps.core import session_binding

from .conftest import PASSWORD, SESSION_TOKENS
from .test_security_audit_authz import _communicator, _db, ws_env  # noqa: F401  (ws_env: fixture)

pytestmark = pytest.mark.django_db
TOKEN = "HTTP_X_MCPANEL_SESSION_TOKEN"


def code(resp: Any) -> str:
    return resp.json()["error"]["code"]


def _signed_in(name: str = "owner") -> tuple[APIClient, str]:
    client = APIClient()
    resp = client.post("/api/auth/login", {"username": name, "password": PASSWORD})
    assert resp.status_code == 200, resp.content
    token = resp.json()["session_token"]
    assert isinstance(token, str) and len(token) >= 40
    client.mcpanel_no_token = True  # (the suite's helper would add the header)
    return client, token


def _cookie_only(client: APIClient) -> APIClient:
    stolen = APIClient(REMOTE_ADDR="203.0.113.7")
    stolen.cookies[settings.SESSION_COOKIE_NAME] = client.cookies[settings.SESSION_COOKIE_NAME].value
    stolen.mcpanel_no_token = True
    return stolen


def test_login_and_setup_answer_the_token_once_and_the_session_keeps_only_its_digest(users: Any) -> None:
    from django.contrib.sessions.models import Session

    client, token = _signed_in()
    data = Session.objects.get(session_key=client.cookies[settings.SESSION_COOKIE_NAME].value).get_decoded()
    assert token not in str(data) and session_binding.token_matches(data, token)
    # the token is not in any later answer
    assert token not in client.get("/api/auth/me", **{TOKEN: token}).content.decode()


def test_setup_answers_a_token(db: Any, settings: Any) -> None:
    settings.MCPANEL_SETUP_TOKEN = ""
    client = APIClient()
    resp = client.post("/api/auth/setup", {"username": "first", "password": "a-Very-long-passphrase"})
    assert resp.status_code == 201 and resp.json()["session_token"]
    client.mcpanel_no_token = True
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", **{TOKEN: resp.json()["session_token"]}).status_code == 200


@pytest.mark.parametrize("method", ["get_me", "get_list", "get_detail", "post_power", "patch_detail", "get_download"])
def test_the_cookie_alone_gets_401_on_safe_unsafe_and_download_routes(
    method: str, users: Any, make_instance: Any, fake_agent: Any
) -> None:
    inst = make_instance()
    fake_agent.put_file(inst.id, "notes.txt", "hi")
    client, token = _signed_in()
    calls = {
        "get_me": lambda c, **h: c.get("/api/auth/me", **h),
        "get_list": lambda c, **h: c.get("/api/instances/", **h),
        "get_detail": lambda c, **h: c.get(f"/api/instances/{inst.id}", **h),
        "post_power": lambda c, **h: c.post(f"/api/instances/{inst.id}/power", {"action": "kill"}, format="json", **h),
        "patch_detail": lambda c, **h: c.patch(f"/api/instances/{inst.id}", {"description": "x"}, format="json", **h),
        "get_download": lambda c, **h: c.get(f"/api/instances/{inst.id}/files/download", {"path": "notes.txt"}, **h),
    }
    call = calls[method]
    for who, headers in (
        ("the tab without its header", {}),
        ("another browser with the cookie", {}),
        ("a wrong token", {TOKEN: token[:-1] + ("A" if token[-1] != "A" else "B")}),
    ):
        c = client if who != "another browser with the cookie" else _cookie_only(client)
        resp = call(c, **headers)
        assert resp.status_code == 401 and code(resp) == "session_token_required", (who, resp.content)
    assert call(client, **{TOKEN: token}).status_code in (200, 202), method


def test_a_session_from_before_the_upgrade_signs_in_again(users: Any) -> None:
    from django.contrib.sessions.backends.db import SessionStore

    client = APIClient()
    client.force_login(users["owner"])  # (the suite's helper gives it a token ...)
    session = SessionStore(client.cookies[settings.SESSION_COOKIE_NAME].value)
    session.pop(session_binding.SESSION_DIGEST_KEY)  # ... which a session of the old release does not have
    session.save()
    resp = client.get("/api/instances/")
    assert resp.status_code == 401 and code(resp) == "session_token_required"
    state = client.get("/api/auth/state").json()
    assert state["authenticated"] is False and state["user"] is None and state["session_token_required"] is True


def test_the_exempt_routes_need_no_header(users: Any) -> None:
    client, token = _signed_in()
    state = client.get("/api/auth/state").json()
    assert state["authenticated"] is False and state["session_token_required"] is True
    state = client.get("/api/auth/state", **{TOKEN: token}).json()
    assert state["authenticated"] is True and state["user"]["username"] == "owner"
    assert client.get("/api/health").status_code in (200, 503)
    assert client.post("/api/auth/logout").status_code == 204
    # a new sign-in from the same browser is a new token; the old one is gone with the old session
    again, new = _signed_in()
    assert new != token


def test_binding_off_lets_the_cookie_through(users: Any, settings: Any) -> None:
    client, _token = _signed_in()
    settings.MCPANEL_SESSION_BINDING = False
    assert _cookie_only(client).get("/api/auth/me").status_code == 200
    assert client.get("/api/auth/state").json()["authenticated"] is True


# --- download tickets ---------------------------------------------------------------------------------------
def _ticket_url(client: APIClient, token: str, url: str) -> str:
    resp = client.post("/api/auth/download-ticket", {"url": url}, format="json", **{TOKEN: token})
    assert resp.status_code == 200, resp.content
    assert resp.json()["expires_in"] == session_binding.TICKET_SECONDS
    return resp.json()["url"]


def test_a_ticket_lets_a_link_download_without_the_header(users: Any, make_instance: Any, fake_agent: Any) -> None:
    inst = make_instance()
    fake_agent.put_file(inst.id, "logs/latest.log", b"hello")
    client, token = _signed_in()
    url = f"/api/instances/{inst.id}/files/download?path=logs%2Flatest.log"
    ticketed = _ticket_url(client, token, url)
    assert ticketed.startswith(url + "&ticket=")
    resp = client.get(ticketed)  # a link: cookie, no header
    assert resp.status_code == 200 and b"".join(resp.streaming_content) == b"hello"
    assert client.get(ticketed).status_code == 200  # (more than once within the 60 s)
    # the other ticket routes
    for other in (f"/api/instances/{inst.id}/files/archive?path=logs", f"/api/instances/{inst.id}/saves/export"):
        assert "ticket=" in _ticket_url(client, token, other)


def test_a_ticket_is_bound_to_its_session_path_query_and_time(
    users: Any, make_instance: Any, fake_agent: Any, monkeypatch: Any
) -> None:
    import time as real_time
    from types import SimpleNamespace

    from django.core import signing

    inst = make_instance()
    fake_agent.put_file(inst.id, "a.txt", b"a")
    fake_agent.put_file(inst.id, "b.txt", b"b")
    client, token = _signed_in()
    ticketed = _ticket_url(client, token, f"/api/instances/{inst.id}/files/download?path=a.txt")
    ticket = ticketed.rsplit("ticket=", 1)[1]
    refused = [
        # another file with this ticket
        client.get(f"/api/instances/{inst.id}/files/download?path=b.txt&ticket={ticket}"),
        # another route
        client.get(f"/api/instances/{inst.id}/files/text?path=a.txt&ticket={ticket}"),
        # another browser holding only the link (no cookie of this session)
        APIClient().get(ticketed),
        # a tampered ticket
        client.get(ticketed[:-3] + "abc"),
    ]
    other, other_token = _signed_in("manager")
    refused.append(other.get(ticketed))  # another session (with its own cookie)
    for resp in refused:
        assert resp.status_code in (401, 403), resp.content
    later = real_time.time() + session_binding.TICKET_SECONDS + 1
    monkeypatch.setattr(signing, "time", SimpleNamespace(time=lambda: later))
    assert client.get(ticketed).status_code == 401  # expired


def test_tickets_only_for_download_routes(users: Any, make_instance: Any) -> None:
    inst = make_instance()
    client, token = _signed_in()
    for url in (
        f"/api/instances/{inst.id}",
        f"/api/instances/{inst.id}/files/text?path=a",
        "https://evil.example/api/x",
        "//evil.example/api/instances/",
        "/not-api/",
        "/api/nope",
    ):
        resp = client.post("/api/auth/download-ticket", {"url": url}, format="json", **{TOKEN: token})
        assert resp.status_code == 400 and code(resp) == "invalid_url", url
    # and the ticket endpoint itself needs the header
    resp = client.post("/api/auth/download-ticket", {"url": f"/api/instances/{inst.id}/files/download?path=a"}, format="json")
    assert resp.status_code == 401


# --- WebSockets -------------------------------------------------------------------------------------------
def _ws_user() -> tuple[Any, str]:
    from apps.accounts.models import Role, User
    from apps.accounts.views import ensure_system_roles
    from apps.instances.models import Instance
    from apps.nodes.models import Node

    from .test_security_audit_authz import _session_cookie

    ensure_system_roles()
    node = Node.objects.create(name="ws-node", agent_url="http://agent.invalid:8700", is_default=True)
    inst = Instance.objects.create(name="WS", node=node, game_port=41997, image="itzg/minecraft-server:stable-java21")
    user = User.objects.create_user("wsbind", password=PASSWORD, role=Role.objects.get(key="operator"))
    return inst, _session_cookie(user)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("path", ["console", "events"])
async def test_websockets_send_nothing_before_the_auth_frame(path: str, ws_env: Any, monkeypatch: Any) -> None:  # noqa: F811
    from apps.console import consumers

    monkeypatch.setattr(consumers, "AUTH_TIMEOUT_SECONDS", 0.5)
    inst, key = await _db(_ws_user)
    url = f"/ws/instances/{inst.id}/console/" if path == "console" else "/ws/events/"
    token = SESSION_TOKENS[key]

    async def closed(comm: Any) -> int:
        out = await comm.receive_output(3)
        assert out["type"] == "websocket.close", out
        return out.get("code")

    # 1. no frame: nothing is sent, then 4401 after the timeout
    comm = _communicator(url, key)
    assert (await comm.connect(auth=False))[0]
    assert await comm.receive_nothing(0.2)
    assert await closed(comm) == 4401
    # 2. a wrong token / another frame first: 4401 at once
    for frame in ({"type": "auth", "token": "wrong"}, {"type": "command", "id": 1, "command": "list"}, {"type": "auth"}):
        comm = _communicator(url, key)
        assert (await comm.connect(auth=False))[0]
        await comm.send_json_to(frame)
        assert await closed(comm) == 4401, frame
    # 3. the token of the session: the socket works
    comm = _communicator(url, key)
    assert (await comm.connect(auth=False))[0]
    await comm.send_json_to({"type": "auth", "token": token})
    if path == "console":
        assert (await comm.receive_json_from(timeout=2))["type"] == "hello"
    else:
        from channels.layers import get_channel_layer

        from apps.console.events import EVENTS_GROUP

        await asyncio.sleep(0.1)
        event = {"type": "panel.event", "event": "instance.status", "payload": {}, "instance_id": str(inst.id)}
        await get_channel_layer().group_send(EVENTS_GROUP, event)
        assert (await comm.receive_json_from(timeout=2))["instance_id"] == str(inst.id)
    await comm.disconnect()
