"""One odd file must not break a file operation (audit agent-files#3, #4, #5, #6, #7).

Real temporary server folders; names that are not UTF-8 are created as bytes.
"""

from __future__ import annotations

import io
import os
import threading
import time
import uuid
import zipfile
from pathlib import Path

import pytest

from mcagent import archive, docker_ops, main
from mcagent.docker_ops import AgentError
from mcagent.safefs import SafeRoot

from .test_security_audit import _run_with_deadline, _wait_op

LATIN1_NAME = b"caf\xe9.txt"  # an old Latin-1 file name: not valid UTF-8


@pytest.fixture
def server(agent_settings, monkeypatch):
    monkeypatch.setattr(docker_ops, "status", lambda _iid: {"running": False})
    iid = uuid.uuid4()
    root = agent_settings.instances_dir / str(iid)
    (root / "world").mkdir(parents=True)
    (root / "world" / "level.dat").write_bytes(b"L")
    return iid, root


def _bad_name(folder: Path) -> bytes:
    path = os.path.join(os.fsencode(folder), LATIN1_NAME)
    with open(path, "wb") as fh:
        fh.write(b"latin-1 named file")
    return path


# -- agent-files#6: listing and hashing a folder with a non-UTF-8 name ---------------------------------
def test_a_folder_with_a_name_that_is_not_utf8_can_still_be_listed(api, auth, server):
    iid, root = server
    _bad_name(root / "world")
    r = api.get(f"/v1/instances/{iid}/files/list?path=world", headers=auth)
    assert r.status_code == 200, r.text
    entries = {e["name"]: e for e in r.json()["entries"]}
    assert set(entries) == {"level.dat", "caf�.txt"}
    assert entries["caf�.txt"]["name_valid"] is False and entries["caf�.txt"]["path"] == "world/caf�.txt"
    assert "name_valid" not in entries["level.dat"]


def test_hashes_and_search_survive_a_name_that_is_not_utf8(api, auth, server):
    iid, root = server
    (root / "mods").mkdir()
    (root / "mods" / "ok.jar").write_bytes(b"jar")
    _bad_name(root / "mods")
    r = api.get(f"/v1/instances/{iid}/files/hashes?path=mods", headers=auth)
    assert r.status_code == 200, r.text
    assert sorted(f["name"] for f in r.json()["files"]) == ["caf�.txt", "ok.jar"]
    r = api.get(f"/v1/instances/{iid}/files/search?q=caf", headers=auth)
    assert r.status_code == 200, r.text


# -- agent-files#7: text that is not UTF-8 is never damaged -------------------------------------------
def test_a_text_file_that_is_not_utf8_is_refused_not_mangled(api, auth, server):
    iid, root = server
    (root / "plugins").mkdir()
    (root / "plugins" / "messages.yml").write_bytes("prefix: '\xa7aCaf\xe9'\n".encode("latin-1"))
    r = api.get(f"/v1/instances/{iid}/files/text?path=plugins/messages.yml", headers=auth)
    assert r.status_code == 415 and r.json()["error"]["code"] == "not_utf8"


def test_a_properties_file_in_latin1_is_read_and_written_back_in_latin1(api, auth, server):
    iid, root = server
    original = "motd=Caf\xe9 \xa7aopen\nlevel-name=bruh\n".encode("latin-1")
    (root / "server.properties").write_bytes(original)
    r = api.get(f"/v1/instances/{iid}/files/text?path=server.properties", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["encoding"] == "iso-8859-1" and body["content"] == original.decode("latin-1")
    changed = body["content"].replace("bruh", "world")
    r = api.put(
        f"/v1/instances/{iid}/files/text",
        json={"path": "server.properties", "content": changed},
        headers=auth,
    )
    assert r.status_code == 200 and r.json()["encoding"] == "iso-8859-1"
    assert (root / "server.properties").read_bytes() == original.replace(b"bruh", b"world")


def test_a_latin1_properties_file_that_gets_a_character_latin1_lacks_becomes_utf8(api, auth, server):
    iid, root = server
    (root / "server.properties").write_bytes("motd=Caf\xe9\n".encode("latin-1"))
    r = api.put(
        f"/v1/instances/{iid}/files/text",
        json={"path": "server.properties", "content": "motd=5€\n"},
        headers=auth,
    )
    assert r.status_code == 200 and r.json()["encoding"] == "utf-8"
    assert (root / "server.properties").read_bytes() == "motd=5€\n".encode()


def test_utf8_files_stay_utf8(api, auth, server):
    iid, root = server
    (root / "server.properties").write_bytes("motd=Café\n".encode())
    body = api.get(f"/v1/instances/{iid}/files/text?path=server.properties", headers=auth).json()
    assert body["encoding"] == "utf-8" and body["content"] == "motd=Café\n"
    api.put(
        f"/v1/instances/{iid}/files/text",
        json={"path": "server.properties", "content": body["content"]},
        headers=auth,
    )
    assert (root / "server.properties").read_bytes() == "motd=Café\n".encode()


# -- agent-files#4: a file that grows while it is downloaded --------------------------------------------
def test_a_growing_file_is_sent_as_it_was_when_opened(api, auth, server, monkeypatch):
    iid, root = server
    log = root / "logs" / "latest.log"
    log.parent.mkdir()
    log.write_bytes(b"x" * 100_000)
    real_open = SafeRoot.open_read

    def open_then_grow(self, rel):  # type: ignore[no-untyped-def]
        cm = real_open(self, rel)
        real_enter = cm.__enter__

        def enter():  # type: ignore[no-untyped-def]
            value = real_enter()
            with open(log, "ab") as fh:  # the server writes on while the download runs
                fh.write(b"y" * 50_000)
            return value

        cm.__enter__ = enter
        return cm

    monkeypatch.setattr(SafeRoot, "open_read", open_then_grow)
    monkeypatch.setattr(main, "DOWNLOAD_CHUNK", 4096)
    r = api.get(f"/v1/instances/{iid}/files/download?path=logs/latest.log", headers=auth)
    assert r.status_code == 200
    assert int(r.headers["content-length"]) == len(r.content) == 100_000 and set(r.content) == {ord("x")}


# -- agent-files#3: the zip export -------------------------------------------------------------------------
def test_old_files_and_odd_names_do_not_abort_the_zip(api, auth, server, monkeypatch):
    iid, root = server
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        old = root / "world" / "old.txt"
        old.write_text("from 1970")
        os.utime(old, (0, 0))
        repro = root / "world" / "repro.txt"
        repro.write_text("1980-01-01 UTC")
        os.utime(repro, (315532800, 315532800))  # 1979-12-31 16:00 in Los Angeles
        _bad_name(root / "world")
        r = api.get(f"/v1/instances/{iid}/files/archive?path=world", headers=auth)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        infos = {i.filename: i for i in zf.infolist()}
        assert set(infos) == {"world/level.dat", "world/old.txt", "world/repro.txt", "world/caf�.txt"}
        assert infos["world/old.txt"].date_time == (1980, 1, 1, 0, 0, 0)
        assert zf.read("world/caf�.txt") == b"latin-1 named file"
        assert zf.read("world/old.txt") == b"from 1970"


def test_zip_dates_are_clamped_to_what_zip_can_store():
    assert archive.zip_date_time(-1e12) == archive.ZIP_MIN_DATE
    assert archive.zip_date_time(1e13) == archive.ZIP_MAX_DATE
    assert archive.zip_date_time(1.7e9)[0] == 2023


def test_a_consumer_that_comes_back_after_the_producer_gave_up_does_not_hang(tmp_path, monkeypatch):
    inst = tmp_path / "inst"
    (inst / "world").mkdir(parents=True)
    for i in range(40):
        (inst / "world" / f"r{i}.mca").write_bytes(os.urandom(64 * 1024))
    monkeypatch.setattr(archive, "ZIP_QUEUE_CHUNKS", 2)
    monkeypatch.setattr(archive, "ZIP_PUT_POLL_SECONDS", 0.05)
    monkeypatch.setattr(archive, "ZIP_STALL_SECONDS", 0.3)
    monkeypatch.setattr(archive, "ZIP_GET_POLL_SECONDS", 0.05)
    before = {t for t in threading.enumerate() if t.name == "zip-stream"}
    gen = archive.stream_zip(SafeRoot(inst), "world")
    assert next(gen)
    (producer,) = [t for t in threading.enumerate() if t.name == "zip-stream" and t not in before]
    producer.join(5)  # it saw no reader for ZIP_STALL_SECONDS and gave up
    assert not producer.is_alive()
    finished, _result, exc = _run_with_deadline(lambda: list(gen), 10)
    assert finished, "the consumer blocked forever after the producer had given up"
    assert isinstance(exc, AgentError) and exc.code == "archive_failed"


# -- agent-files#5: a failed import does not leave the uploaded archive behind --------------------------
def test_a_failed_import_removes_the_uploaded_archive(api, auth, server):
    iid, root = server
    upload = root / ".mcpanel-import-0123abcd.zip"
    upload.write_bytes(b"PK\x03\x04 this is not a complete zip")
    r = api.post(
        f"/v1/instances/{iid}/files/extract",
        json={"path": upload.name, "delete_archive": True},
        headers=auth,
    )
    op = _wait_op(api, auth, r.json()["operation"]["id"])
    assert op["status"] == "failed", op
    assert not upload.exists()


def test_a_failed_extract_keeps_an_archive_the_caller_wants_to_keep(api, auth, server):
    iid, root = server
    mine = root / "my-upload.zip"
    mine.write_bytes(b"not a zip")
    r = api.post(f"/v1/instances/{iid}/files/extract", json={"path": mine.name}, headers=auth)
    op = _wait_op(api, auth, r.json()["operation"]["id"])
    assert op["status"] == "failed" and mine.exists()


# -- review r3 #25: a download the client gives up on closes its file at once ----------------------
def _open_fds_on(path: Path) -> int:
    n = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            if os.readlink(f"/proc/self/fd/{fd}") == str(path):
                n += 1
        except OSError:
            continue
    return n


async def _serve_until_the_client_leaves(response, *, spec: str, chunks_before: int = 1) -> None:
    """Drive the answer like uvicorn does: ASGI 2.4 (``send`` fails once the client is gone) or
    older (``receive`` says ``http.disconnect``)."""
    import asyncio

    from starlette.requests import ClientDisconnect

    sent = 0
    gone = asyncio.Event()

    async def send(message):
        nonlocal sent
        if message["type"] == "http.response.body" and message.get("body"):
            sent += 1
            if sent > chunks_before:
                gone.set()
                if spec == "2.4":
                    raise OSError("connection reset by peer")
                await asyncio.sleep(10)  # stalls until the disconnect cancels it

    async def receive():
        await gone.wait()
        return {"type": "http.disconnect"}

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": spec}}
    try:
        await response(scope, receive, send)
    except ClientDisconnect:
        assert spec == "2.4"


@pytest.mark.parametrize("spec", ["2.4", "2.3"])
def test_a_cancelled_download_closes_the_file_at_once(agent_settings, server, spec):
    import asyncio

    iid, _root = server
    big = agent_settings.instances_dir / str(iid) / "big.bin"
    big.write_bytes(os.urandom(main.DOWNLOAD_CHUNK * 4))

    async def scenario():
        response = await main.files_download(iid, "big.bin", settings=agent_settings)
        assert _open_fds_on(big) == 1
        await _serve_until_the_client_leaves(response, spec=spec)
        # the answer object (and so the generator) is still referenced: only an explicit close helps
        return response, _open_fds_on(big)

    response, still_open = asyncio.run(scenario())
    assert still_open == 0
    del response


def test_a_download_whose_answer_never_started_closes_the_file(agent_settings, server):
    import asyncio

    iid, _root = server
    big = agent_settings.instances_dir / str(iid) / "big.bin"
    big.write_bytes(b"x" * 1000)

    async def scenario():
        response = await main.files_download(iid, "big.bin", settings=agent_settings)

        async def send(_message):
            raise OSError("connection reset by peer")  # gone before the first byte

        async def receive():
            return {"type": "http.disconnect"}

        with pytest.raises(Exception):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        return response, _open_fds_on(big)

    response, still_open = asyncio.run(scenario())
    assert still_open == 0
    del response


def _zip_threads() -> int:
    return sum(t.name == "zip-stream" and t.is_alive() for t in threading.enumerate())


def test_a_cancelled_zip_download_stops_its_producer(agent_settings, server):
    import asyncio

    iid, root = server
    for i in range(40):
        (root / "world" / f"r{i}.mca").write_bytes(os.urandom(256 * 1024))  # more than the queue holds

    async def scenario():
        response = await main.files_archive(iid, path=["world"], settings=agent_settings)
        await _serve_until_the_client_leaves(response, spec="2.4")
        deadline = time.monotonic() + 5
        while _zip_threads() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        return response, _zip_threads()  # the answer is still referenced here

    response, producers = asyncio.run(scenario())
    assert producers == 0
    del response
