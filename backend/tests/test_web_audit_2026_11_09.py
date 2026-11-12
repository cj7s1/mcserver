"""Regression tests for the open findings of the 2026-11-09 audit, work package "backend-web".

Each section names its finding. The ASGI tests drive the real application (mcpanel.asgi) the
way uvicorn does, with transactional databases because Django runs the views in other threads.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import io
import json
import logging
import os
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path
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
from apps.instances import properties
from apps.nodes.client import AgentClient, AgentError, AgentTimeout, AgentUnavailable

from .conftest import PASSWORD

BACKEND = Path(__file__).resolve().parents[1]
MiB = 1024 * 1024
CSRF = "abcdefghijklmnopqrstuvwxyzABCDEF"  # an unmasked CSRF secret (32 characters)


def err(resp: Any) -> str:
    return resp.json()["error"]["code"]


# --------------------------------------------------------------------------------------
# ASGI plumbing
# --------------------------------------------------------------------------------------
def _asgi_app() -> Any:
    from mcpanel.asgi import application

    return application


def _scope(method: str, path: str, *, query: str = "", headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", b"testserver"), *(headers or [])],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


def _login(user: Any) -> str:
    from django.conf import settings
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_login(user)
    return client.cookies[settings.SESSION_COOKIE_NAME].value


def _cookies(session: str | None) -> list[tuple[bytes, bytes]]:
    from django.conf import settings

    from tests.conftest import SESSION_TOKENS

    parts = [f"{settings.CSRF_COOKIE_NAME}={CSRF}"]
    if session:
        parts.append(f"{settings.SESSION_COOKIE_NAME}={session}")
    headers = [(b"cookie", "; ".join(parts).encode()), (b"x-csrftoken", CSRF.encode())]
    if session and session in SESSION_TOKENS:  # (D-1: the session's token, as the frontend sends it)
        headers.append((b"x-mcpanel-session-token", SESSION_TOKENS[session].encode()))
    return headers


async def _response(comm: ApplicationCommunicator, timeout: float = 10) -> tuple[int, dict[bytes, bytes], bytes]:
    start = await comm.receive_output(timeout)
    assert start["type"] == "http.response.start", start
    body = b""
    while True:
        msg = await comm.receive_output(timeout)
        body += msg.get("body", b"")
        if not msg.get("more_body"):
            break
    return start["status"], dict(start["headers"]), body


@pytest.fixture
def web_connections(monkeypatch):  # type: ignore[no-untyped-def]
    """Database connections as the web process has them: closed after every request (no
    persistent ones), so the request threads leave nothing open behind."""
    from django.db import connections

    monkeypatch.setitem(connections.settings["default"], "CONN_MAX_AGE", web_conn_max_age())
    yield
    connections.close_all()


def web_conn_max_age() -> int:
    """CONN_MAX_AGE of a process that loaded the settings through mcpanel/asgi.py (as uvicorn does)."""
    return _settings_in_subprocess("import mcpanel.asgi")


def _settings_in_subprocess(statement: str) -> int:
    code = (
        "import os, django\n"
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mcpanel.settings')\n"
        f"{statement}\n"
        "from django.conf import settings\n"
        "print(settings.DATABASES['default']['CONN_MAX_AGE'])\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in ("MCPANEL_PROCESS_ROLE", "POSTGRES_CONN_MAX_AGE")}
    env["MCPANEL_TESTING"] = "1"
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, check=True)
    return int(out.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------------------
# backend-api#2: no persistent database connections in the web process
# --------------------------------------------------------------------------------------
def test_web_process_has_no_persistent_connections_but_workers_do():
    assert web_conn_max_age() == 0
    assert _settings_in_subprocess("django.setup(); import mcpanel.celery") == 60


def _db_sessions() -> int:
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
        return int(cur.fetchone()[0])


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_sequential_web_requests_leave_no_database_sessions(web_connections):
    app = _asgi_app()
    before = await sync_to_async(_db_sessions)()
    gc.disable()  # orphaned connections were closed only by the garbage collector
    try:
        for _ in range(12):
            comm = ApplicationCommunicator(app, _scope("GET", "/api/health"))
            await comm.send_input({"type": "http.request", "body": b"", "more_body": False})
            status, _h, _b = await _response(comm)
            assert status in (200, 503)
            await comm.wait(5)
        after = await sync_to_async(_db_sessions)()
    finally:
        gc.enable()
    assert after - before <= 1, (before, after)


# --------------------------------------------------------------------------------------
# backend-api#1: downloads stream chunk by chunk under ASGI
# --------------------------------------------------------------------------------------
class SlowAgentStream:
    """An agent download: ``count`` chunks of 1 MiB, produced only when asked for."""

    def __init__(self, count: int = 96) -> None:
        self.count = count
        self.produced = 0
        self.closed = False

    def chunks(self) -> Any:
        while self.produced < self.count and not self.closed:
            self.produced += 1
            time.sleep(0.001)
            yield b"\0" * MiB

    def close(self) -> None:  # the httpx.Response
        self.closed = True


@pytest.fixture
def download_env(make_user, make_instance, fake_agent, monkeypatch, web_connections):  # type: ignore[no-untyped-def]
    stream = SlowAgentStream()

    def download(_client, instance_id, path, *, endpoint="download"):  # type: ignore[no-untyped-def]
        return stream.chunks(), None, stream

    monkeypatch.setattr(AgentClient, "download", download)
    inst = make_instance()
    session = _login(make_user("dl-owner", owner=True))
    return inst, session, stream


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("suffix", "query"), [("files/download", "path=big.bin"), ("files/archive", "path=world"), ("files/archive", "path=a&path=b")]
)
async def test_download_streams_while_the_agent_still_sends(download_env, suffix, query):
    inst, session, stream = download_env
    comm = ApplicationCommunicator(
        _asgi_app(), _scope("GET", f"/api/instances/{inst.id}/{suffix}", query=query, headers=_cookies(session))
    )
    tracemalloc.start()
    try:
        await comm.send_input({"type": "http.request", "body": b"", "more_body": False})
        start = await comm.receive_output(10)
        assert start["type"] == "http.response.start" and start["status"] == 200, start
        first = await comm.receive_output(10)
        assert first["type"] == "http.response.body" and first["body"]
        produced_at_first_byte = stream.produced
        received = len(first["body"])
        while True:
            msg = await comm.receive_output(10)
            received += len(msg.get("body", b""))
            if not msg.get("more_body"):
                break
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    await comm.wait(5)
    assert produced_at_first_byte <= 2, produced_at_first_byte  # before: all 96 chunks were read first
    assert received == 96 * MiB
    assert peak < 24 * MiB, peak  # before: about the whole download (96 MiB)
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancelled_download_closes_the_agent_stream(download_env):
    inst, session, stream = download_env
    comm = ApplicationCommunicator(
        _asgi_app(), _scope("GET", f"/api/instances/{inst.id}/files/download", query="path=big.bin", headers=_cookies(session))
    )
    await comm.send_input({"type": "http.request", "body": b"", "more_body": False})
    assert (await comm.receive_output(10))["status"] == 200
    assert (await comm.receive_output(10))["body"]
    await comm.send_input({"type": "http.disconnect"})
    with contextlib.suppress(Exception):
        await comm.wait(10)
    for _ in range(100):
        if stream.closed:
            break
        await asyncio.sleep(0.05)
    assert stream.closed
    assert stream.produced < stream.count  # the rest was never read from the agent


def test_download_still_works_for_synchronous_clients(clients, make_instance, fake_agent):
    inst = make_instance()
    fake_agent.put_file(inst.id, "logs/latest.log", b"hello")
    resp = clients["owner"].get(f"/api/instances/{inst.id}/files/download", {"path": "logs/latest.log"})
    assert resp.status_code == 200 and b"".join(resp.streaming_content) == b"hello"


# --------------------------------------------------------------------------------------
# backend-api#3: body limits before Django reads the body; one copy of an upload on disk
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_oversized_declared_body_is_refused_without_reading_it(web_connections, tmp_path):
    with override_settings(FILE_UPLOAD_TEMP_DIR=str(tmp_path)):
        comm = ApplicationCommunicator(
            _asgi_app(), _scope("POST", "/api/auth/login", headers=[(b"content-length", str(64 * MiB).encode())])
        )
        # nothing of the body was sent: the answer comes from the headers alone
        status, headers, body = await _response(comm)
    assert status == 413 and json.loads(body)["error"]["code"] == "request_too_large"
    assert headers[b"connection"] == b"close"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_chunked_body_is_cut_off_at_the_limit(web_connections, tmp_path):
    with override_settings(FILE_UPLOAD_TEMP_DIR=str(tmp_path), MCPANEL_MAX_REQUEST_BYTES=1 * MiB):
        comm = ApplicationCommunicator(_asgi_app(), _scope("POST", "/api/auth/login"))  # no Content-Length
        sent = 0
        answer = None
        for _ in range(64):  # up to 64 MiB, unless the panel answers before
            await comm.send_input({"type": "http.request", "body": b"x" * (256 * 1024), "more_body": True})
            sent += 256 * 1024
            if not await comm.receive_nothing(0.05):  # (receive_output would cancel the app on a timeout)
                answer = await comm.receive_output(1)
                break
        assert answer is not None and answer["type"] == "http.response.start"
        assert answer["status"] == 413
    assert sent <= 2 * MiB


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_upload_size_limits_need_a_session(make_instance, make_user, fake_agent, web_connections):
    inst = await sync_to_async(make_instance)()
    path = f"/api/instances/{inst.id}/files/upload"
    with override_settings(MCPANEL_MAX_REQUEST_BYTES=1 * MiB, MCPANEL_MAX_UPLOAD_BYTES=8 * MiB):
        cases = [
            (None, 2 * MiB, 413, "request_too_large"),  # no session: at most the small limit
            ("forged", 2 * MiB, 413, "request_too_large"),
            ("session", 9 * MiB, 413, "file_too_large"),  # above MCPANEL_MAX_UPLOAD_BYTES
        ]
        session = await sync_to_async(lambda: _login(make_user("up-owner", owner=True)))()
        for who, length, status, code in cases:
            key = session if who == "session" else ("x" * 32 if who == "forged" else None)
            headers = [*_cookies(key), (b"content-length", str(length).encode())]
            comm = ApplicationCommunicator(_asgi_app(), _scope("POST", path, headers=headers))
            got, _h, body = await _response(comm)
            assert (got, json.loads(body)["error"]["code"]) == (status, code), who
        # an upload without a declared length is refused before it is read (as before, but now really)
        comm = ApplicationCommunicator(_asgi_app(), _scope("POST", path, headers=_cookies(session)))
        got, _h, body = await _response(comm)
        assert got == 411 and json.loads(body)["error"]["code"] == "length_required"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_upload_is_stored_once_while_it_is_forwarded(
    make_instance, make_user, fake_agent, web_connections, monkeypatch, tmp_path
):
    from apps.core import asgi as core_asgi

    inst = await sync_to_async(make_instance)()
    fake_agent.prepare(inst.id)
    session = await sync_to_async(lambda: _login(make_user("up2-owner", owner=True)))()
    spools: list[Any] = []
    original = core_asgi.PanelASGIHandler.read_body

    async def read_body(self, receive):  # type: ignore[no-untyped-def]
        body = await original(self, receive)
        spools.append(body)
        return body

    monkeypatch.setattr(core_asgi.PanelASGIHandler, "read_body", read_body)
    seen: dict[str, Any] = {}

    def on_upload(instance_id, path, chunks, overwrite=False, sha256=None):  # type: ignore[no-untyped-def]
        body = spools[-1]
        body.seek(0, io.SEEK_END)
        seen["spool_bytes"] = body.tell()
        seen["temp_files"] = sorted(p.name for p in tmp_path.iterdir())

    fake_agent.hooks["upload"] = on_upload
    payload = encode_multipart(BOUNDARY, {"file": SimpleUploadedFile("world.bin", b"w" * (6 * MiB))})
    headers = [*_cookies(session), (b"content-type", MULTIPART_CONTENT.encode()), (b"content-length", str(len(payload)).encode())]
    with override_settings(FILE_UPLOAD_TEMP_DIR=str(tmp_path), MCPANEL_MAX_REQUEST_BYTES=1 * MiB):
        comm = ApplicationCommunicator(_asgi_app(), _scope("POST", f"/api/instances/{inst.id}/files/upload", headers=headers))
        for i in range(0, len(payload), 256 * 1024):
            part = payload[i : i + 256 * 1024]
            await comm.send_input({"type": "http.request", "body": part, "more_body": i + len(part) < len(payload)})
        status, _h, body = await _response(comm, timeout=20)
    assert status == 201, body
    assert fake_agent.get_file(inst.id, "world.bin") == b"w" * (6 * MiB)
    # while the file went to the agent (above FILE_UPLOAD_MAX_MEMORY_SIZE, so both copies were on
    # disk before): the parsed upload exists once, the request body spool is empty
    assert seen["spool_bytes"] == 0
    assert len(seen["temp_files"]) == 1 and seen["temp_files"][0].endswith(".upload.bin")


def test_upload_temp_dir_is_a_dedicated_directory():
    from django.conf import settings

    assert settings.FILE_UPLOAD_TEMP_DIR == os.environ.get("MCPANEL_UPLOAD_TMP", "/tmp/mcpanel-uploads")
    assert Path(settings.FILE_UPLOAD_TEMP_DIR).is_dir()


# --------------------------------------------------------------------------------------
# backend-api#4: more than 100 files in one upload
# --------------------------------------------------------------------------------------
def test_upload_of_many_files_works(clients, make_instance, fake_agent):
    inst = make_instance()
    fake_agent.prepare(inst.id)
    fake_agent.mkdir(inst.id, "mods")
    files = [SimpleUploadedFile(f"mod-{i:03d}.jar", b"jar") for i in range(150)]
    resp = clients["owner"].post(f"/api/instances/{inst.id}/files/upload?path=mods", {"file": files}, format="multipart")
    assert resp.status_code == 201, resp.content
    assert len(resp.json()["uploaded"]) == 150 and resp.json()["failed"] == []
    assert fake_agent.get_file(inst.id, "mods/mod-149.jar") == b"jar"


def test_too_many_files_is_a_clear_413(clients, make_instance, fake_agent, settings):
    settings.DATA_UPLOAD_MAX_NUMBER_FILES = 5
    inst = make_instance()
    files = [SimpleUploadedFile(f"f{i}.txt", b"x") for i in range(6)]
    resp = clients["owner"].post(f"/api/instances/{inst.id}/files/upload", {"file": files}, format="multipart")
    assert resp.status_code == 413 and err(resp) == "too_many_files"
    assert resp.json()["error"]["details"] == {"max_files": 5}


def test_upload_reports_each_file_that_could_not_be_stored(clients, make_instance, fake_agent):
    inst = make_instance()
    fake_agent.put_file(inst.id, "b.txt", b"old")
    files = [SimpleUploadedFile("a.txt", b"a"), SimpleUploadedFile("b.txt", b"b")]
    resp = clients["owner"].post(f"/api/instances/{inst.id}/files/upload", {"file": files}, format="multipart")
    assert resp.status_code == 201
    body = resp.json()
    assert [u["path"] for u in body["uploaded"]] == ["a.txt"]
    assert body["failed"] == [{"name": "b.txt", "path": "b.txt", "code": "exists", "message": "File already exists"}]
    assert fake_agent.get_file(inst.id, "b.txt") == b"old"
    # a single file that fails is still an error, as before
    one = clients["owner"].post(
        f"/api/instances/{inst.id}/files/upload", {"file": [SimpleUploadedFile("b.txt", b"c")]}, format="multipart"
    )
    assert one.status_code == 409 and err(one) == "exists"


# --------------------------------------------------------------------------------------
# backend-api#6: encodings of server.properties and text files
# --------------------------------------------------------------------------------------
def test_properties_are_written_as_ascii_with_unicode_escapes():
    assert properties._format("motd", "§aCafé") == "motd=\\u00a7aCaf\\u00e9"
    assert properties._format("motd", "x😀") == "motd=x\\ud83d\\ude00"
    text = properties.update("# Café\nmotd=old\nname=\\é\n", {"motd": "§6Hi"})
    assert text.isascii()
    assert properties.parse(text) == {"motd": "§6Hi", "name": "é"}
    assert properties.parse(properties.render({"motd": "x😀"})) == {"motd": "x😀"}
    assert properties.render({"motd": "Ü"}).isascii()


def test_properties_patch_writes_escapes_and_keeps_latin1_files(clients, make_instance, fake_agent):
    inst = make_instance()
    # an older server's file: ISO-8859-1 bytes (0xA7 = §), not valid UTF-8
    fake_agent.put_file(inst.id, "server.properties", b"motd=\xa76My Server\nwhite-list=false\n")
    shown = clients["owner"].get(f"/api/instances/{inst.id}/properties").json()
    assert shown["values"]["motd"] == "§6My Server"
    resp = clients["owner"].patch(f"/api/instances/{inst.id}/properties", {"values": {"white-list": True}}, format="json")
    assert resp.status_code == 200, resp.content
    data = fake_agent.get_file(inst.id, "server.properties")
    assert data == b"motd=\\u00a76My Server\nwhite-list=true\n"
    # the raw editor's text is written the same way
    raw = clients["owner"].patch(
        f"/api/instances/{inst.id}/properties", {"raw": "motd=Caf\u00e9\nwhite-list=true\n"}, format="json"
    )
    assert raw.status_code == 200 and raw.json()["values"]["motd"] == "Café"
    assert fake_agent.get_file(inst.id, "server.properties") == b"motd=Caf\\u00e9\nwhite-list=true\n"


def test_text_editor_reports_and_protects_files_that_are_not_utf8(clients, make_instance, fake_agent):
    inst = make_instance()
    base = f"/api/instances/{inst.id}/files/text"
    fake_agent.put_file(inst.id, "plugins/x/config.yml", b"name: Caf\xe9\n")
    fake_agent.put_file(inst.id, "ok.txt", "real \ufffd replacement char\n".encode())
    got = clients["owner"].get(base, {"path": "plugins/x/config.yml"}).json()
    assert got["binary_or_non_utf8"] is True and "\ufffd" in got["content"]
    assert clients["owner"].get(base, {"path": "ok.txt"}).json()["binary_or_non_utf8"] is False
    put = clients["owner"].put(base, {"path": "plugins/x/config.yml", "content": got["content"]}, format="json")
    assert put.status_code == 409 and err(put) == "non_utf8_file"
    assert fake_agent.get_file(inst.id, "plugins/x/config.yml") == b"name: Caf\xe9\n"
    confirmed = clients["owner"].put(
        base, {"path": "plugins/x/config.yml", "content": "name: Cafe\n", "confirm_non_utf8": True}, format="json"
    )
    assert confirmed.status_code == 200 and fake_agent.get_file(inst.id, "plugins/x/config.yml") == b"name: Cafe\n"
    # new files and UTF-8 files save without asking
    assert clients["owner"].put(base, {"path": "new.txt", "content": "é"}, format="json").status_code == 200
    assert clients["owner"].put(base, {"path": "ok.txt", "content": "x"}, format="json").status_code == 200


# --------------------------------------------------------------------------------------
# backend-api#7: long file operations
# --------------------------------------------------------------------------------------
def test_file_operations_wait_long_and_report_still_running(clients, make_instance, fake_agent):
    inst = make_instance()
    fake_agent.put_file(inst.id, "bluemap/tiles/a.png", b"x")
    resp = clients["owner"].post(
        f"/api/instances/{inst.id}/files/delete", {"paths": ["bluemap"], "recursive": True}, format="json"
    )
    assert resp.status_code == 200
    from apps.nodes.client import FILE_OPERATION_TIMEOUT

    assert fake_agent.called("delete_files")[0][1]["timeout"] == FILE_OPERATION_TIMEOUT
    fake_agent.failures["delete_files"] = AgentTimeout("The node agent did not respond in time (POST /files/delete)")
    slow = clients["owner"].post(f"/api/instances/{inst.id}/files/delete", {"paths": ["world"], "recursive": True}, format="json")
    assert slow.status_code == 202 and slow.json()["still_running"] is True
    entry = AuditEvent.objects.filter(action="files.delete").order_by("-id").first()
    assert entry.details["still_running"] is True and entry.details["paths"] == ["world"]
    fake_agent.failures["rename"] = AgentTimeout("The node agent did not respond in time (POST /files/rename)")
    moved = clients["owner"].post(
        f"/api/instances/{inst.id}/files/rename", {"source": "world", "destination": "old"}, format="json"
    )
    assert moved.status_code == 202 and moved.json()["still_running"] is True
    # an agent that cannot be reached is still an error
    fake_agent.failures["delete_files"] = AgentUnavailable("Cannot reach the node agent")
    down = clients["owner"].post(f"/api/instances/{inst.id}/files/delete", {"paths": ["x"]}, format="json")
    assert down.status_code == 503
    assert AuditEvent.objects.filter(action="files.delete", success=False).count() == 1


@respx.mock
def test_agent_read_timeout_is_a_timeout_and_connect_timeout_is_not(node):
    client = AgentClient(node)
    route = respx.post(f"{node.agent_url}/v1/instances/00000000-0000-0000-0000-000000000001/files/delete")
    route.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(AgentTimeout):
        client.delete_files("00000000-0000-0000-0000-000000000001", ["x"], timeout=1)
    route.mock(side_effect=httpx.ConnectTimeout("down"))
    with pytest.raises(AgentUnavailable) as exc:
        client.delete_files("00000000-0000-0000-0000-000000000001", ["x"], timeout=1)
    assert not isinstance(exc.value, AgentTimeout)


# --------------------------------------------------------------------------------------
# frontend-instance-b#3: a typed `stop` runs the panel's Stop
# --------------------------------------------------------------------------------------
def _ws_setup(codes: list[str]) -> tuple[Any, Any, str]:
    from apps.accounts.models import InstanceAccess, Role, User
    from apps.instances.models import Instance
    from apps.nodes.models import Node

    node = Node.objects.create(name="ws-node", agent_url="http://agent.invalid:8700", is_default=True)
    inst = Instance.objects.create(name="WS", node=node, game_port=41998, image="itzg/minecraft-server:stable-java21")
    role = Role.objects.create(key="ws-role", name="WS role", permissions=codes)
    user = User.objects.create_user("wsstop", password=PASSWORD)
    InstanceAccess.objects.create(user=user, instance=inst, role=role)
    return inst, user, _login(user)


@pytest.fixture
def ws_env(monkeypatch):  # type: ignore[no-untyped-def]
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


async def _close_db() -> None:
    from channels.db import database_sync_to_async
    from django.db import connections

    await database_sync_to_async(connections.close_all)()


@pytest.mark.parametrize("text", ["stop", "/stop", " STOP ", "//Stop"])
def test_stop_command_detection(text):
    from apps.console.consumers import is_stop_command

    assert is_stop_command(text)
    assert not is_stop_command("stopwatch") and not is_stop_command("say stop")


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_typed_stop_enqueues_the_stop_job(ws_env):
    from apps.jobs.models import Job

    from .test_security_audit_authz import WsClient

    inst, _user, key = await sync_to_async(_ws_setup)(
        ["instance.view", "instance.console", "instance.command", "instance.control"]
    )
    comm = WsClient(f"/ws/instances/{inst.id}/console/", key)
    assert (await comm.connect())[0]
    await comm.receive_json_from()  # hello
    await comm.send_json_to({"type": "command", "id": "c1", "command": " /STOP "})
    notice = await comm.receive_json_from()
    result = await comm.receive_json_from()
    assert notice["type"] == "notice" and notice["level"] == "info" and "Stop button" in notice["message"]
    assert notice["id"] == "c1"  # the same id as the command_result that follows
    assert result == {"type": "command_result", "id": "c1", "ok": True, "command": "STOP", "output": notice["message"]}
    assert ws_env == []  # nothing went to RCON
    job = await sync_to_async(lambda: Job.objects.get(instance=inst))()
    assert job.kind == "stop" and notice["job_id"] == str(job.id)
    entry = await sync_to_async(lambda: AuditEvent.objects.get(action="instance.stop"))()
    assert entry.details == {"job": str(job.id), "via": "console"} and entry.actor_username == "wsstop"
    await comm.disconnect()
    await _close_db()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_typed_stop_without_control_permission_is_refused(ws_env):
    from apps.jobs.models import Job

    from .test_security_audit_authz import WsClient

    inst, _user, key = await sync_to_async(_ws_setup)(["instance.view", "instance.console", "instance.command"])
    comm = WsClient(f"/ws/instances/{inst.id}/console/", key)
    assert (await comm.connect())[0]
    await comm.receive_json_from()
    await comm.send_json_to({"type": "command", "id": 7, "command": "stop"})
    notice = await comm.receive_json_from()
    result = await comm.receive_json_from()
    assert notice["type"] == "notice" and notice["level"] == "error" and notice["id"] == 7
    assert result["ok"] is False and result["code"] == "stop_refused" and result["id"] == 7
    assert ws_env == [] and not await sync_to_async(Job.objects.exists)()
    await comm.send_json_to({"type": "command", "id": 8, "command": "list"})
    assert (await comm.receive_json_from())["ok"] is True and ws_env == ["list"]
    await comm.disconnect()
    await _close_db()


# --------------------------------------------------------------------------------------
# backend-realtime#3: consumers clean up however they end
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_console_ignores_frames_that_are_not_json_objects(ws_env):
    from .test_security_audit_authz import WsClient

    inst, _user, key = await sync_to_async(_ws_setup)(["instance.view", "instance.console", "instance.command"])
    comm = WsClient(f"/ws/instances/{inst.id}/console/", key)
    assert (await comm.connect())[0]
    await comm.receive_json_from()
    await comm.comm.send_input({"type": "websocket.receive", "text": "[]"})
    await comm.comm.send_input({"type": "websocket.receive", "text": "{not json"})
    await comm.comm.send_input({"type": "websocket.receive", "bytes": b"\x00\x01"})
    await comm.send_json_to({"type": "command", "id": 1, "command": "list"})
    assert (await comm.receive_json_from())["ok"] is True  # the consumer is still there
    await comm.disconnect()
    await _close_db()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_console_consumer_ending_with_an_exception_stops_its_tasks(ws_env, monkeypatch):
    from apps.console import consumers

    from .test_security_audit_authz import WsClient

    seen: list[Any] = []
    connect = consumers.ConsoleConsumer.connect

    async def recording_connect(self):  # type: ignore[no-untyped-def]
        seen.append(self)
        await connect(self)

    async def broken_command(user, instance, command):  # type: ignore[no-untyped-def]
        raise RuntimeError("the channel layer is down")

    monkeypatch.setattr(consumers.ConsoleConsumer, "connect", recording_connect)
    monkeypatch.setattr(consumers, "_run_command", broken_command)
    inst, _user, key = await sync_to_async(_ws_setup)(["instance.view", "instance.console", "instance.command"])
    comm = WsClient(f"/ws/instances/{inst.id}/console/", key)
    assert (await comm.connect())[0]
    await comm.receive_json_from()
    await comm.send_json_to({"type": "command", "id": 1, "command": "list"})
    with pytest.raises(RuntimeError):
        await comm.comm.wait(5)
    consumer = seen[0]
    assert consumer.relay_task.done() and consumer.watch_task.done()  # before: both kept running
    await _close_db()


@pytest.mark.asyncio
async def test_console_relay_stops_when_the_browser_is_gone(monkeypatch):
    from apps.console import consumers

    connects: list[str] = []

    class FakeAgentSocket:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
            return False

        def __aiter__(self):  # type: ignore[no-untyped-def]
            async def lines():  # type: ignore[no-untyped-def]
                yield json.dumps({"type": "log", "line": "hello"})
                await asyncio.Event().wait()

            return lines()

    def connect(url, **kwargs):  # type: ignore[no-untyped-def]
        connects.append(url)
        return FakeAgentSocket()

    monkeypatch.setattr(consumers.websockets, "connect", connect)

    class Client:
        headers: dict[str, str] = {}

        def ws_url(self, path):  # type: ignore[no-untyped-def]
            return "ws://agent" + path

    monkeypatch.setattr("apps.nodes.client.AgentClient", lambda node: Client())
    consumer = consumers.ConsoleConsumer()
    consumer._closing = False
    consumer.instance_id = "x"
    consumer.instance = type("I", (), {"node": None, "id": "x"})()
    sent: list[dict[str, Any]] = []

    async def send_json(content, close=False):  # type: ignore[no-untyped-def]
        if content.get("type") == "log":
            raise ConnectionError("client disconnected")
        sent.append(content)

    consumer.send_json = send_json  # type: ignore[method-assign]
    await asyncio.wait_for(consumer._relay(), timeout=5)  # returns instead of reconnecting forever
    assert connects == ["ws://agent/v1/instances/x/logs/stream?tail=300"]


# --------------------------------------------------------------------------------------
# backend-realtime#4: live events keep coming after the group expiry
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_events_socket_renews_its_group_membership(ws_env, monkeypatch):
    from channels.layers import get_channel_layer

    from apps.console import consumers
    from apps.console.events import EVENTS_GROUP

    from .test_security_audit_authz import WsClient

    layer = get_channel_layer()
    monkeypatch.setattr(layer, "group_expiry", 1)
    monkeypatch.setattr(consumers, "GROUP_REFRESH_SECONDS", 0.2)
    _inst, _user, key = await sync_to_async(_ws_setup)(["instance.view"])
    comm = WsClient("/ws/events/", key)
    assert (await comm.connect())[0]
    await asyncio.sleep(2.5)  # longer than the group expiry
    await layer.group_send(
        EVENTS_GROUP, {"type": "panel.event", "event": "job.progress", "payload": {"p": 1}, "instance_id": None}
    )
    assert (await comm.receive_json_from(timeout=2))["type"] == "job.progress"
    await comm.disconnect()
    await _close_db()


# --------------------------------------------------------------------------------------
# backend-realtime#6: `list` output of EssentialsX and old servers
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("output", "online", "maximum", "names"),
    [
        ("There are 2 of a max of 20 players online: Alice, Bob", 2, 20, ["Alice", "Bob"]),
        ("There are 2 of a max 20 players online: Alice, Bob", 2, 20, ["Alice", "Bob"]),
        ("There are 0 of a max of 20 players online: ", 0, 20, []),
        (
            "There are 3 out of maximum 20 players online.\nAdmins: Alice\ndefault: Bob, [AFK]Carol",
            3,
            20,
            ["Alice", "Bob", "Carol"],
        ),
        (
            "There are 2/1 out of maximum 20 players online.\nAdmins: [HIDDEN]Dave, Alice\ndefault: Bob",
            3,
            20,
            ["Dave", "Alice", "Bob"],
        ),
        ("There are 2/20 players online:\nAlice, Bob", 2, 20, ["Alice", "Bob"]),
        ("There are 2/20 players online:Alice, Bob", 2, 20, ["Alice", "Bob"]),
        ("There are 1 out of maximum 10 players online.\ndefault: ~Nick Name", 1, 10, []),
    ],
)
def test_player_list_formats(output, online, maximum, names):
    from apps.players.service import parse_list

    assert parse_list(output) == {"online": online, "max": maximum, "players": names}


# --------------------------------------------------------------------------------------
# backend-auth#4: a createsuperuser account has every permission
# --------------------------------------------------------------------------------------
def test_superuser_without_the_owner_flag_can_use_the_panel(make_user, client_for, make_instance, users):
    from apps.accounts.models import User
    from apps.accounts.permissions import ALL_CODES, INSTANCE_CODES

    inst = make_instance()
    rescue = User.objects.create_superuser("rescue", password=PASSWORD)
    assert rescue.global_permissions() == ALL_CODES and rescue.instance_permissions(inst.pk) == INSTANCE_CODES
    c = client_for(rescue)
    assert c.get("/api/users").status_code == 200
    assert c.get("/api/system/settings").status_code == 200
    assert [i["id"] for i in c.get("/api/instances/").json()] == [str(inst.id)]
    assert c.get("/api/auth/me").json()["permissions"]
    rescue.is_active = False
    assert rescue.global_permissions() == frozenset()


# --------------------------------------------------------------------------------------
# SPA caching: hashed assets for good, index.html revalidated
# --------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_spa_cache_headers(tmp_path):
    from django.test import Client

    (tmp_path / "index.html").write_text("<!doctype html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-BxYz12_a.js").write_text("1")
    (tmp_path / "assets" / "plain.js").write_text("2")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    with override_settings(WHITENOISE_ROOT=tmp_path, FRONTEND_DIST=tmp_path):
        client = Client()
        hashed = client.get("/assets/index-BxYz12_a.js")
        assert hashed.status_code == 200 and hashed["Cache-Control"] == "public, max-age=31536000, immutable"
        for path in ("/", "/index.html", "/servers/123"):
            resp = client.get(path)
            assert resp.status_code == 200 and resp["Cache-Control"] == "no-cache", path
        assert "immutable" not in client.get("/assets/plain.js").get("Cache-Control", "")
        assert "immutable" not in client.get("/favicon.svg").get("Cache-Control", "")


# --------------------------------------------------------------------------------------
# Diagnostics: an expected agent 404/409 is no error in the log
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "status", "code"),
    [("status", 404, "not_found"), ("status", 409, "conflict"), ("logs", 404, "no_container"), ("logs", 409, "not_running")],
)
def test_diagnostics_do_not_log_errors_for_expected_agent_answers(
    clients, make_instance, fake_agent, caplog, method, status, code
):
    inst = make_instance()
    fake_agent.failures[method] = AgentError("expected", code, status)
    with caplog.at_level(logging.INFO):
        resp = clients["owner"].get(f"/api/instances/{inst.id}/diagnostics")
    assert resp.status_code in (200, status)
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


# --------------------------------------------------------------------------------------
# backend-modpacks#5: the CurseForge key is checked when it is saved
# --------------------------------------------------------------------------------------
CF = "https://api.curseforge.com"


@respx.mock
def test_curseforge_key_is_checked_before_it_is_saved(clients, db):
    from apps.core.models import SystemSetting

    route = respx.get(f"{CF}/v1/games/432")
    route.mock(return_value=httpx.Response(403, json={}))
    bad = clients["owner"].patch("/api/system/settings", {"secrets": {"curseforge_api_key": "nope"}}, format="json")
    assert bad.status_code == 400 and err(bad) == "invalid_curseforge_key"
    assert route.calls.last.request.headers["x-api-key"] == "nope"
    assert SystemSetting.get_secret("curseforge_api_key") in (None, "")
    route.mock(return_value=httpx.Response(200, json={"data": {"id": 432}}))
    ok = clients["owner"].patch("/api/system/settings", {"secrets": {"curseforge_api_key": " $2a$10$good "}}, format="json")
    assert ok.status_code == 200 and ok.json()["warnings"] == []
    assert SystemSetting.get_secret("curseforge_api_key") == "$2a$10$good"
    route.mock(side_effect=httpx.ConnectError("offline"))
    offline = clients["owner"].patch("/api/system/settings", {"secrets": {"curseforge_api_key": "other"}}, format="json")
    assert offline.status_code == 200 and "could not be asked" in offline.json()["warnings"][0]
    assert SystemSetting.get_secret("curseforge_api_key") == "other"
    # clearing the key needs no check
    calls = route.call_count
    assert (
        clients["owner"].patch("/api/system/settings", {"secrets": {"curseforge_api_key": ""}}, format="json").status_code == 200
    )
    assert route.call_count == calls


# --------------------------------------------------------------------------------------
# backend-modpacks#3: CurseForge release lists read every page
# --------------------------------------------------------------------------------------
def _cf_file(fid: int, day: int) -> dict[str, Any]:
    return {
        "id": fid,
        "modId": 123,
        "displayName": f"Pack {fid}",
        "fileName": f"pack-{fid}.zip",
        "gameVersions": ["1.20.1", "Forge"],
        "fileDate": f"2026-01-01T00:{day // 60:02d}:{day % 60:02d}Z",
    }


@respx.mock
def test_curseforge_releases_read_all_pages(db, settings):
    from apps.core.models import SystemSetting
    from apps.modpacks.providers import CurseForge

    SystemSetting.set_secret("curseforge_api_key", "k")
    files = [_cf_file(1000 + i, i) for i in range(120)]

    def page(request):  # type: ignore[no-untyped-def]
        index = int(request.url.params["index"])
        size = int(request.url.params["pageSize"])
        return httpx.Response(200, json={"data": files[index : index + size], "pagination": {"index": index, "totalCount": 120}})

    route = respx.get(f"{CF}/v1/mods/123/files").mock(side_effect=page)
    releases = CurseForge().releases("123")
    assert len(releases) == 120 and route.call_count == 3
    assert CurseForge().releases("123") == releases and route.call_count == 3  # cached


@respx.mock
def test_modpack_page_counts_updates_when_the_pin_is_not_listed(clients, make_instance, db):
    from apps.core.models import SystemSetting

    SystemSetting.set_secret("curseforge_api_key", "k")
    inst = make_instance(
        server_type="AUTO_CURSEFORGE",
        modpack_provider="curseforge",
        modpack_project_id="123",
        modpack_version_id="1",
        modpack_loader="forge",
    )
    listed = [{**_cf_file(2000 + i, i), "fileDate": f"2026-05-{1 + i:02d}T00:00:00Z"} for i in range(3)]
    respx.get(f"{CF}/v1/mods/123/files").mock(return_value=httpx.Response(200, json={"data": listed}))
    pinned = {**_cf_file(1, 0), "fileDate": "2026-05-02T12:00:00Z"}
    respx.get(f"{CF}/v1/mods/123/files/1").mock(return_value=httpx.Response(200, json={"data": pinned}))
    body = clients["owner"].get(f"/api/instances/{inst.id}/modpack").json()
    assert body["current"]["id"] == "1" and body["updates_available"] == 1  # only the file of 2026-05-03 is newer


# --------------------------------------------------------------------------------------
# backend-modpacks#4: keyless CurseForge packs record their project and loader
# --------------------------------------------------------------------------------------
MANIFEST = {
    "fileId": 5001,
    "fileName": "BMC3.zip",
    "modId": 77,
    "modpackName": "Better MC",
    "modpackVersion": "v31",
    "minecraftVersion": "1.20.1",
    "modLoaderId": "fabric-0.15.11",
    "files": ["mods/a.jar"],
}


def test_file_pinned_keyless_pack_records_its_project_and_loader(make_instance, fake_agent):
    from apps.modpacks.pinning import pin_installed_release

    inst = make_instance(
        server_type="AUTO_CURSEFORGE", modpack_provider="curseforge", modpack_slug="bmc", modpack_version_id="5001"
    )
    fake_agent.put_file(inst.id, ".curseforge-manifest.json", json.dumps(MANIFEST))
    assert pin_installed_release(inst) is True
    inst.refresh_from_db()
    assert (inst.modpack_version_id, inst.modpack_project_id, inst.modpack_loader) == ("5001", "77", "fabric")
    assert pin_installed_release(inst) is False  # nothing left to record


def test_modpack_page_retries_pinning_and_never_answers_500(clients, make_instance, fake_agent):
    inst = make_instance(server_type="AUTO_CURSEFORGE", modpack_provider="curseforge", modpack_slug="bmc")
    resp = clients["owner"].get(f"/api/instances/{inst.id}/modpack")
    assert resp.status_code == 409 and err(resp) == "project_not_recorded"
    from django.core.cache import cache

    cache.clear()  # the retry is rate-limited per server
    fake_agent.put_file(inst.id, ".curseforge-manifest.json", json.dumps(MANIFEST))
    with respx.mock:
        respx.get(f"{CF}/v1/mods/77/files").mock(return_value=httpx.Response(200, json={"data": []}))
        from apps.core.models import SystemSetting

        SystemSetting.set_secret("curseforge_api_key", "k")
        respx.get(f"{CF}/v1/mods/77/files/5001").mock(
            return_value=httpx.Response(200, json={"data": {**_cf_file(5001, 0), "modId": 77}})
        )
        body = clients["owner"].get(f"/api/instances/{inst.id}/modpack")
    assert body.status_code == 200, body.content
    inst.refresh_from_db()
    assert inst.modpack_project_id == "77" and inst.modpack_version_id == "5001" and inst.modpack_loader == "fabric"


@pytest.mark.parametrize("provider", ["curseforge", "ftb"])
def test_non_numeric_project_ids_are_404(clients, provider, db):
    from apps.core.models import SystemSetting

    SystemSetting.set_secret("curseforge_api_key", "k")
    resp = clients["owner"].get(f"/api/modpacks/{provider}/projects/not-a-number")
    assert resp.status_code == 404 and err(resp) == "provider_not_found"


def test_existing_keyless_pack_gets_its_loader_from_the_pinned_manifest(make_instance):
    from apps.content.service import context_for

    inst = make_instance(
        server_type="AUTO_CURSEFORGE",
        modpack_loader="",
        modpack_meta={"pinned_from_manifest": {"fileId": 1, "modLoaderId": "neoforge-21.1.77"}},
    )
    assert context_for(inst, require_version=False).loaders == ("neoforge",)
