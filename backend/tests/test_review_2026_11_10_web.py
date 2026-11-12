# ruff: noqa: F811 (the fixtures imported from other test modules are parameters of the tests)
"""Web requests (review of 2026-11-10): pack files the runtime must not re-install (finding 1), the
upload gate's cookies and sessions (2, 18), CurseForge keys (19), server.properties from the file
editor (20) and closing the agent's response of a download (21)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import httpx
import pytest
import respx
from asgiref.sync import sync_to_async
from asgiref.testing import ApplicationCommunicator
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from django.test.utils import override_settings

from apps.audit.models import AuditEvent
from apps.content import packfiles
from apps.instances.spec import build_env, current_spec_hash
from apps.nodes.client import AgentTimeout

from .test_web_audit_2026_11_09 import CSRF, MiB, _asgi_app, _login, _response, _scope, err, web_connections  # noqa: F401

CF = "https://api.curseforge.com"
MANIFEST = {
    "fileId": 5001,
    "files": ["mods/packmod-1.0.jar", "mods/other.jar", "mods/third.jar", "config/packmod.toml", "plugins/p.jar"],
}


# --------------------------------------------------------------------------------------
# finding 1: only a single jar removed directly in mods/ (or plugins/) is recorded
# --------------------------------------------------------------------------------------
@pytest.fixture
def pack(make_instance, fake_agent):
    inst = make_instance(server_type="AUTO_CURSEFORGE", modpack_loader="fabric", modpack_version_id="5001")
    fake_agent.put_file(inst.id, ".curseforge-manifest.json", json.dumps(MANIFEST))
    for path in MANIFEST["files"]:
        fake_agent.put_file(inst.id, path, b"x")
    fake_agent.put_file(inst.id, "mods/mine.jar", b"m")
    return inst


def _op(client: Any, inst: Any, op: str, body: dict[str, Any]) -> Any:
    return client.post(f"/api/instances/{inst.id}/files/{op}", body, format="json")


def recorded(inst: Any) -> list[str]:
    inst.refresh_from_db()
    return list(packfiles.recorded(inst).get("paths") or [])


@pytest.mark.django_db
def test_folders_config_files_and_several_files_are_never_recorded(clients, pack, fake_agent):
    c = clients["owner"]
    assert _op(c, pack, "delete", {"paths": ["config/packmod.toml"]}).status_code == 200  # a config file
    assert _op(c, pack, "delete", {"paths": ["mods/other.jar", "mods/third.jar"]}).status_code == 200  # several at once
    assert _op(c, pack, "rename", {"source": "plugins", "destination": "plugins-off"}).status_code == 200  # a folder
    assert _op(c, pack, "rename", {"source": "mods", "destination": "mods-old"}).status_code == 200
    assert _op(c, pack, "delete", {"paths": ["mods-old"], "recursive": True}).status_code == 200
    assert recorded(pack) == []
    assert "CF_IGNORE_MISSING_FILES" not in build_env(pack)


@pytest.mark.django_db
def test_one_jar_deleted_in_mods_is_recorded_but_not_when_the_answer_timed_out(clients, pack, fake_agent):
    c = clients["owner"]
    fake_agent.failures["delete_files"] = AgentTimeout("The node agent did not respond in time (POST /files/delete)")
    slow = _op(c, pack, "delete", {"paths": ["mods/other.jar"]})
    assert slow.status_code == 202 and slow.json()["still_running"] is True
    fake_agent.failures["rename"] = AgentTimeout("The node agent did not respond in time (POST /files/rename)")
    assert _op(c, pack, "rename", {"source": "mods/third.jar", "destination": "mods/third.jar.off"}).status_code == 202
    assert recorded(pack) == []  # the outcome is unknown
    fake_agent.failures.clear()
    assert _op(c, pack, "delete", {"paths": ["mods/other.jar"]}).status_code == 200
    assert _op(c, pack, "delete", {"paths": ["mods/mine.jar"]}).status_code == 200  # not a pack file
    assert recorded(pack) == ["mods/other.jar"]
    assert build_env(pack)["CF_IGNORE_MISSING_FILES"] == "mods/other.jar"


def test_recordable_paths():
    assert packfiles.recordable("mods/a.jar") and packfiles.recordable("./plugins/B.JAR")
    for path in ("mods", "mods/", "mods/sub/a.jar", "config/a.jar", "mods/a.toml", "a.jar", "mods/.jar", "world/mods/a.jar"):
        assert not packfiles.recordable(path), path


@pytest.mark.django_db
def test_the_modpack_tab_lists_the_record_and_lets_the_pack_reinstall(clients, pack, fake_agent, make_user, client_for):
    c = clients["owner"]
    url = f"/api/instances/{pack.id}/modpack/removed-files"
    assert c.get(url).json() == {"supported": True, "release": None, "paths": [], "applies": True, "restart_required": False}
    assert _op(c, pack, "delete", {"paths": ["mods/other.jar"]}).status_code == 200
    from apps.instances.models import Instance

    # the server has a container made from the configuration before the record
    Instance.objects.filter(pk=pack.pk).update(container_state="running", container_spec_hash=current_spec_hash(pack))
    assert _op(c, pack, "delete", {"paths": ["mods/third.jar"]}).status_code == 200
    listed = c.get(url).json()
    assert listed["paths"] == ["mods/other.jar", "mods/third.jar"] and listed["release"] == "5001" and listed["applies"]
    assert listed["restart_required"] is True
    # only who manages the content may drop it
    viewer = client_for(make_user("pack-viewer", role="viewer"))
    assert viewer.delete(url).status_code in (403, 404)
    pack.refresh_from_db()
    revision = pack.config_revision
    with_record = current_spec_hash(pack)
    resp = c.delete(url)
    assert resp.status_code == 200, resp.content
    assert resp.json()["cleared"] == ["mods/other.jar", "mods/third.jar"] and resp.json()["paths"] == []
    pack.refresh_from_db()
    assert recorded(pack) == [] and pack.config_revision == revision + 1
    assert "CF_IGNORE_MISSING_FILES" not in build_env(pack)
    assert current_spec_hash(pack) != with_record  # the next restart recreates the container
    event = AuditEvent.objects.get(action="modpack.removed_files_cleared")
    assert event.details["count"] == 2 and event.details["paths"] == ["mods/other.jar", "mods/third.jar"]


@pytest.mark.django_db
def test_servers_without_a_pack_manifest_say_so(clients, make_instance):
    inst = make_instance(server_type="FABRIC")
    assert clients["owner"].get(f"/api/instances/{inst.id}/modpack/removed-files").json()["supported"] is False


# --------------------------------------------------------------------------------------
# findings 2 and 18: the upload gate
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_json_cookie_of_another_app_does_not_hide_the_session(make_instance, make_user, fake_agent, web_connections):
    from django.conf import settings

    inst = await sync_to_async(make_instance)()
    await sync_to_async(fake_agent.prepare)(inst.id)
    session = await sync_to_async(lambda: _login(make_user("json-cookie", owner=True)))()
    payload = encode_multipart(BOUNDARY, {"file": SimpleUploadedFile("big.bin", b"b" * (2 * MiB))})
    cookie = (
        f'prefs={{"theme":"dark","list":[1,2]}}; {settings.CSRF_COOKIE_NAME}={CSRF}; {settings.SESSION_COOKIE_NAME}={session}'
    )
    from tests.conftest import SESSION_TOKENS

    headers = [
        (b"cookie", cookie.encode()),
        (b"x-csrftoken", CSRF.encode()),
        (b"x-mcpanel-session-token", SESSION_TOKENS[session].encode()),  # (D-1)
        (b"content-type", MULTIPART_CONTENT.encode()),
        (b"content-length", str(len(payload)).encode()),
    ]
    with override_settings(MCPANEL_MAX_REQUEST_BYTES=1 * MiB):
        comm = ApplicationCommunicator(_asgi_app(), _scope("POST", f"/api/instances/{inst.id}/files/upload", headers=headers))
        for i in range(0, len(payload), 256 * 1024):
            part = payload[i : i + 256 * 1024]
            await comm.send_input({"type": "http.request", "body": part, "more_body": i + len(part) < len(payload)})
        status, _h, body = await _response(comm, timeout=20)
    assert status == 201, body
    assert fake_agent.get_file(inst.id, "big.bin") == b"b" * (2 * MiB)
    from django.db import connections

    # the connection the helper thread opened for the fixtures must not outlive this test's database
    await sync_to_async(connections.close_all)()


@pytest.mark.django_db(transaction=True)
def test_the_size_gate_needs_an_active_user_and_a_session_of_the_current_password(make_user):
    from apps.core.asgi import _session_key, _signed_in

    from tests.conftest import SESSION_TOKENS

    user = make_user("gate-user", owner=True)
    key = _login(user)
    assert _signed_in(key, SESSION_TOKENS[key]) is True
    assert _signed_in(key) is False  # (D-1: the cookie alone is not enough, R-10 of the branch review)
    assert _session_key({"headers": [(b"cookie", f'a={{"x":1}}; mcpanel_session={key}'.encode())]}) == key
    user.set_password("another-long-password-1")
    user.save()
    assert _signed_in(key, SESSION_TOKENS[key]) is False  # the password changed: Django ends the session
    fresh = _login(user)
    assert _signed_in(fresh, SESSION_TOKENS[fresh]) is True
    type(user).objects.filter(pk=user.pk).update(is_active=False)
    assert _signed_in(fresh) is False
    assert _signed_in("x" * 32) is False


# --------------------------------------------------------------------------------------
# finding 19: a CurseForge key with a character a header cannot carry
# --------------------------------------------------------------------------------------
@respx.mock
@pytest.mark.django_db
@pytest.mark.parametrize("key", ["$2a$10$caf\u00e9", "$2a$10$ab\ncd", "$2a$10$ab cd", "\u2026"])
def test_a_curseforge_key_with_odd_characters_is_refused_without_asking(clients, key):
    from apps.core.models import SystemSetting

    route = respx.get(f"{CF}/v1/games/432").mock(return_value=httpx.Response(200, json={"data": {"id": 432}}))
    resp = clients["owner"].patch("/api/system/settings", {"secrets": {"curseforge_api_key": key}}, format="json")
    assert resp.status_code == 400 and err(resp) == "invalid_curseforge_key", resp.content
    assert route.call_count == 0
    assert SystemSetting.get_secret("curseforge_api_key") in (None, "")


@respx.mock
@pytest.mark.django_db
def test_a_stored_key_with_odd_characters_is_an_error_not_a_crash(settings):
    from apps.core.errors import ApiError
    from apps.modpacks.providers import CurseForge

    settings.CURSEFORGE_API_KEY = "$2a$10$caf\u00e9"
    route = respx.get(url__startswith=CF).mock(return_value=httpx.Response(200, json={"data": []}))
    with pytest.raises(ApiError) as exc:
        CurseForge()._get("/v1/mods/search")
    assert exc.value.error_code == "invalid_curseforge_key" and route.call_count == 0


# --------------------------------------------------------------------------------------
# finding 20: server.properties from the file editor, written like the Properties tab
# --------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_an_older_agent_gets_ascii_escapes_for_a_latin1_server_properties(clients, make_instance, fake_agent):
    inst = make_instance()
    fake_agent.strict_text = False  # an agent from before strict decoding: lossy U+FFFD, no "encoding"
    fake_agent.put_file(inst.id, "server.properties", b"motd=Caf\xe9\nwhite-list=false\n")
    # the Properties tab reads it as ISO-8859-1 (properties.read_file)
    assert clients["owner"].get(f"/api/instances/{inst.id}/properties").json()["values"]["motd"] == "Café"
    base = f"/api/instances/{inst.id}/files/text"
    got = clients["owner"].get(base, {"path": "server.properties"}).json()
    assert got["content"] == "motd=Café\nwhite-list=false\n" and got["binary_or_non_utf8"] is False
    assert got["encoding"] == "iso-8859-1"
    put = clients["owner"].put(base, {"path": "server.properties", "content": "motd=Café ü §\nwhite-list=false\n"}, format="json")
    assert put.status_code == 200, put.content
    assert fake_agent.get_file(inst.id, "server.properties") == b"motd=Caf\\u00e9 \\u00fc \\u00a7\nwhite-list=false\n"
    # the Properties tab reads back what was typed
    assert clients["owner"].get(f"/api/instances/{inst.id}/properties").json()["values"]["motd"] == "Café ü §"


@pytest.mark.django_db
def test_the_editor_writes_server_properties_as_ascii_like_the_properties_tab(clients, make_instance, fake_agent):
    inst = make_instance()
    base = f"/api/instances/{inst.id}/files/text"
    fake_agent.put_file(inst.id, "server.properties", "motd=x\n")  # UTF-8 (plain ASCII)
    assert clients["owner"].put(base, {"path": "server.properties", "content": "motd=é😀\n"}, format="json").status_code == 200
    assert fake_agent.get_file(inst.id, "server.properties") == b"motd=\\u00e9\\ud83d\\ude00\n"
    # a file the agent keeps in ISO-8859-1 stays ISO-8859-1
    fake_agent.put_file(inst.id, "server.properties", b"motd=\xa7a\n")
    assert clients["owner"].put(base, {"path": "server.properties", "content": "motd=§aé€\n"}, format="json").status_code == 200
    assert fake_agent.get_file(inst.id, "server.properties") == b"motd=\xa7a\xe9\\u20ac\n"
    # other text files keep what was typed
    assert clients["owner"].put(base, {"path": "notes.txt", "content": "é\n"}, format="json").status_code == 200
    assert fake_agent.get_file(inst.id, "notes.txt") == "é\n".encode()


# --------------------------------------------------------------------------------------
# finding 21: a download the browser leaves closes the agent's response at once
# --------------------------------------------------------------------------------------
class _Upstream:
    def __init__(self) -> None:
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()


def _agent_chunks(upstream: _Upstream):
    yield b"first"
    # the next read only ends when its connection is closed (or after 30 s)
    upstream.closed.wait(30)
    raise httpx.ReadError("connection closed")


@pytest.mark.asyncio
async def test_a_download_that_is_left_closes_the_agent_response_explicitly():
    from apps.core.streaming import AgentStreamingResponse

    upstream = _Upstream()
    response = AgentStreamingResponse(_agent_chunks(upstream), upstream)
    stream = response.__aiter__()
    try:
        assert await stream.__anext__() == b"first"
        reading = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0.2)  # the next read waits in a worker thread
        started = time.monotonic()
        reading.cancel()  # the browser went away
        with pytest.raises(asyncio.CancelledError):
            await reading
        assert upstream.closed.is_set()  # closed by the stream itself, not by response.close() later
        assert time.monotonic() - started < 5  # not the abandon wait of a read nobody ended
    finally:
        upstream.closed.set()
