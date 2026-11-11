"""Regression tests for the agent security-audit findings (one section per finding).

Tests that would block forever on the unfixed code run the blocking part in a helper
thread with a deadline, so a regression fails instead of hanging the suite.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import socket
import struct
import sys
import tarfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

import docker.errors
import pytest

from mcagent import archive, docker_ops, main, rcon
from mcagent.config import Settings, get_settings
from mcagent.docker_ops import AgentError
from mcagent.safefs import PathError, SafeRoot

from .conftest import TOKEN


@pytest.fixture
def clean_env(monkeypatch):
    for var in list(os.environ):
        if var.startswith("AGENT_"):
            monkeypatch.delenv(var)


def _run_with_deadline(fn, timeout: float):  # type: ignore[no-untyped-def]
    """Run ``fn`` in a thread; returns (finished, result, exception)."""
    box: dict = {}

    def target() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # reported to the test
            box["exc"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    return not t.is_alive(), box.get("result"), box.get("exc")


def _wait_op(client, auth, op_id: str, timeout: float = 60) -> dict:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        op = client.get(f"/v1/operations/{op_id}", headers=auth).json()
        if op["status"] in ("succeeded", "failed"):
            return op
        time.sleep(0.05)
    raise AssertionError("operation did not finish")


# =====================================================================================
# 1. RCON responses are bounded in size and time
# =====================================================================================
def _packet(req_id: int, ptype: int, payload: bytes) -> bytes:
    body = struct.pack("<ii", req_id, ptype) + payload + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


async def _read_request(reader: asyncio.StreamReader) -> int:
    (length,) = struct.unpack("<i", await reader.readexactly(4))
    data = await reader.readexactly(length)
    return struct.unpack("<i", data[:4])[0]


async def _malicious_rcon(behaviour):  # type: ignore[no-untyped-def]
    async def handle(reader, writer):  # type: ignore[no-untyped-def]
        try:
            auth_id = await _read_request(reader)
            writer.write(_packet(auth_id, 2, b""))
            await writer.drain()
            cmd_id = await _read_request(reader)
            await behaviour(cmd_id, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def test_rcon_fragment_flood_is_truncated(monkeypatch):
    monkeypatch.setattr(rcon, "MAX_RESPONSE_BYTES", 256 * 1024, raising=False)

    async def flood(cmd_id, writer):  # type: ignore[no-untyped-def]
        while True:  # endless full-size fragments, as fast as the client reads
            writer.write(_packet(cmd_id, 0, b"A" * 4096))
            await writer.drain()

    async def scenario() -> str:
        server, port = await _malicious_rcon(flood)
        async with server:
            return await asyncio.wait_for(rcon.run_command("127.0.0.1", port, "pw", "list", timeout=10), 20)

    out = asyncio.run(scenario())
    assert out.endswith(rcon.TRUNCATED_NOTE)
    assert len(out.encode()) <= 256 * 1024 + len(rcon.TRUNCATED_NOTE)


def test_rcon_fragment_trickle_respects_the_command_deadline():
    async def trickle(cmd_id, writer):  # type: ignore[no-untyped-def]
        while True:  # each fragment arrives within the continuation timeout, forever
            writer.write(_packet(cmd_id, 0, b"B" * 4096))
            await writer.drain()
            await asyncio.sleep(0.2)

    async def scenario() -> tuple[str, float]:
        server, port = await _malicious_rcon(trickle)
        async with server:
            started = time.monotonic()
            out = await asyncio.wait_for(rcon.run_command("127.0.0.1", port, "pw", "list", timeout=1.5), 15)
            return out, time.monotonic() - started

    out, elapsed = asyncio.run(scenario())
    assert elapsed < 4
    assert out.endswith(rcon.TRUNCATED_NOTE)


def test_rcon_connection_reset_is_an_rcon_error():
    async def reset(cmd_id, writer):  # type: ignore[no-untyped-def]
        sock = writer.get_extra_info("socket")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # close with RST
        writer.transport.abort()

    async def scenario() -> None:
        server, port = await _malicious_rcon(reset)
        async with server:
            await rcon.run_command("127.0.0.1", port, "pw", "save-all flush", timeout=5)

    with pytest.raises(rcon.RconError):
        asyncio.run(scenario())


# =====================================================================================
# 2. save-on always follows save-off (backups and snapshots)
# =====================================================================================
@pytest.fixture
def running_server(agent_settings, monkeypatch):
    """A 'running' instance whose RCON commands are recorded; ``fail`` maps command -> exception."""
    iid = uuid.uuid4()
    (agent_settings.instances_dir / str(iid)).mkdir()
    sent: list[str] = []
    fail: dict[str, BaseException] = {}

    async def fake_run_command(host, port, password, command, timeout=30.0):  # type: ignore[no-untyped-def]
        sent.append(command)
        if command in fail:
            raise fail[command]
        return ""

    monkeypatch.setattr(docker_ops, "status", lambda _iid: {"running": True})
    monkeypatch.setattr(docker_ops, "rcon_endpoint", lambda _s, _iid: ("127.0.0.1", 25575, "pw"))
    monkeypatch.setattr(main, "run_command", fake_run_command)
    monkeypatch.setattr(main, "SAVE_SETTLE_SECONDS", 0, raising=False)
    monkeypatch.setattr(main, "SAVE_ON_RETRY_SECONDS", 0, raising=False)
    return iid, sent, fail


class _FakeRestic:
    error: BaseException | None = None

    def __init__(self, settings, instance_id):  # type: ignore[no-untyped-def]
        pass

    def backup(self, source, tags, excludes, progress=None):  # type: ignore[no-untyped-def]
        if self.error is not None:
            raise self.error
        return {"snapshot_id": "0123abcd", "warnings": []}


@pytest.mark.parametrize("consistency", ["auto", "require"])
def test_backup_resumes_saving_when_flush_fails(api, auth, running_server, monkeypatch, consistency):
    iid, sent, fail = running_server
    monkeypatch.setattr(main, "Restic", _FakeRestic)
    fail["save-all flush"] = rcon.RconError("Timed out waiting for RCON response")
    r = api.post(
        f"/v1/instances/{iid}/backups", json={"backup_id": "b1", "consistency": consistency}, headers=auth
    )
    op = _wait_op(api, auth, r.json()["operation"]["id"])
    assert sent == ["save-off", "save-all flush", "save-on"]
    if consistency == "require":
        assert op["status"] == "failed" and op["error_code"] == "consistency_failed"
    else:
        assert op["status"] == "succeeded"
        assert op["result"]["consistent"] is False  # the world was not flushed
        assert any("may be inconsistent" in w for w in op["result"]["warnings"])


def test_backup_resumes_saving_when_restic_fails(api, auth, running_server, monkeypatch):
    iid, sent, _fail = running_server
    monkeypatch.setattr(main, "Restic", _FakeRestic)
    monkeypatch.setattr(_FakeRestic, "error", AgentError("restic timed out", "restic_timeout", 504))
    r = api.post(f"/v1/instances/{iid}/backups", json={"backup_id": "b2"}, headers=auth)
    op = _wait_op(api, auth, r.json()["operation"]["id"])
    assert op["status"] == "failed" and op["error_code"] == "restic_timeout"
    assert sent == ["save-off", "save-all flush", "save-on"]


@pytest.mark.parametrize(
    ("consistency", "error"),
    [
        ("auto", rcon.RconError("Timed out waiting for RCON response")),
        ("require", rcon.RconError("Timed out waiting for RCON response")),
        ("auto", ConnectionResetError("reset by peer")),  # not an RconError: used to escape
        ("auto", docker.errors.APIError("container is gone")),
    ],
)
def test_snapshot_resumes_saving_when_flush_fails(api, auth, running_server, monkeypatch, consistency, error):
    from mcagent.snapshots import BtrfsSnapshots

    iid, sent, fail = running_server
    created: list[dict] = []
    monkeypatch.setattr(BtrfsSnapshots, "require_ready", lambda self: None)
    monkeypatch.setattr(
        BtrfsSnapshots, "create", lambda self, label="", meta=None: created.append(meta) or {"name": "s"}
    )
    fail["save-all flush"] = error
    r = api.post(f"/v1/instances/{iid}/snapshots", json={"consistency": consistency}, headers=auth)
    op = _wait_op(api, auth, r.json()["operation"]["id"])
    assert sent == ["save-off", "save-all flush", "save-on"]
    if consistency == "require":
        assert op["status"] == "failed" and op["error_code"] == "consistency_failed"
        assert created == []
    else:
        assert op["status"] == "succeeded"
        assert created == [{"consistent": False, "kind": "manual"}]


def test_saving_resumed_even_if_save_off_reply_is_lost(api, auth, running_server, monkeypatch):
    iid, sent, fail = running_server
    monkeypatch.setattr(main, "Restic", _FakeRestic)
    fail["save-off"] = rcon.RconError("Timed out waiting for RCON response")  # may still have applied
    r = api.post(f"/v1/instances/{iid}/backups", json={"backup_id": "b3"}, headers=auth)
    op = _wait_op(api, auth, r.json()["operation"]["id"])
    assert op["status"] == "succeeded"
    assert sent == ["save-off", "save-on"]


# =====================================================================================
# 3. FIFOs and other special files never block a read
# =====================================================================================
@pytest.fixture
def fifo_root(tmp_path):
    inst = tmp_path / "inst"
    inst.mkdir()
    os.mkfifo(inst / "server.properties")
    yield SafeRoot(inst), inst
    # if a regression left a reader blocked on the FIFO, release it
    try:
        fd = os.open(inst / "server.properties", os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
    except OSError:
        pass


def test_open_read_refuses_a_fifo_without_blocking(fifo_root):
    sr, _inst = fifo_root
    finished, _result, exc = _run_with_deadline(lambda: sr.read_bytes("server.properties", 1024), 5)
    assert finished, "reading a FIFO blocked"
    assert isinstance(exc, PathError) and exc.code == "not_a_file"


def test_open_read_refuses_a_unix_socket(tmp_path):
    inst = tmp_path / "inst"
    inst.mkdir()
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(inst / "mod.sock"))
    try:
        with pytest.raises(PathError) as exc:
            SafeRoot(inst).read_bytes("mod.sock", 1024)
        assert exc.value.code == "not_a_file"
    finally:
        sock.close()


def test_hashing_a_fifo_is_skipped_without_blocking(fifo_root):
    from mcagent import hashcache

    sr, _inst = fifo_root
    finished, result, exc = _run_with_deadline(
        lambda: hashcache._hash_file(sr, "server.properties", "sha1", None), 5
    )
    assert finished, "hashing a FIFO blocked"
    assert exc is None and result is None


def test_text_endpoint_on_a_fifo_fails_fast(api, auth, agent_settings):
    iid = uuid.uuid4()
    inst = agent_settings.instances_dir / str(iid)
    inst.mkdir()
    os.mkfifo(inst / "server.properties")
    try:
        finished, r, exc = _run_with_deadline(
            lambda: api.get(f"/v1/instances/{iid}/files/text?path=server.properties", headers=auth), 10
        )
        assert finished and exc is None, "the agent blocked on a FIFO"
        assert r.status_code == 400 and r.json()["error"]["code"] == "not_a_file"
    finally:
        with pytest.raises(OSError):  # nobody is (still) reading it
            os.close(os.open(inst / "server.properties", os.O_WRONLY | os.O_NONBLOCK))


def test_clone_skips_special_files(api, auth, agent_settings, monkeypatch):
    monkeypatch.setattr(docker_ops, "status", lambda _iid: {"running": False})
    src, dst = uuid.uuid4(), uuid.uuid4()
    inst = agent_settings.instances_dir / str(src)
    (inst / "world").mkdir(parents=True)
    (inst / "world" / "level.dat").write_bytes(b"L")
    (inst / "server.properties").write_text("motd=x\n")
    os.mkfifo(inst / "plugins.fifo")
    os.symlink("server.properties", inst / "link")
    r = api.post(f"/v1/instances/{src}/clone", json={"target_instance_id": str(dst)}, headers=auth)
    op = _wait_op(api, auth, r.json()["operation"]["id"])
    assert op["status"] == "succeeded", op
    out = agent_settings.instances_dir / str(dst)
    assert (out / "world" / "level.dat").read_bytes() == b"L"
    assert (out / "server.properties").read_text() == "motd=x\n"
    assert os.path.islink(out / "link")
    assert not os.path.lexists(out / "plugins.fifo")


# =====================================================================================
# 4. Console log streaming is bounded
# =====================================================================================
class _FakeLogStream:
    def __init__(self, chunks):  # type: ignore[no-untyped-def]
        self._it = iter(chunks)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return self

    def __next__(self) -> bytes:
        return next(self._it)

    def close(self) -> None:
        pass


class _FakeContainer:
    def __init__(self, chunks):  # type: ignore[no-untyped-def]
        self.status = "running"
        self.chunks = chunks
        self.calls = 0

    def reload(self) -> None:
        pass

    def logs(self, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 1:
            return _FakeLogStream(self.chunks)
        self.status = "exited"
        return _FakeLogStream([])


def _collect_lines(api, auth, iid, until: str, limit: int = 2000) -> list[str]:  # type: ignore[no-untyped-def]
    lines: list[str] = []
    with api.websocket_connect(f"/v1/instances/{iid}/logs/stream?tail=10", headers=auth) as ws:
        for _ in range(limit):
            msg = ws.receive_json()
            if msg["type"] == "logs":
                lines.extend(msg["lines"])
                if lines and lines[-1] == until:
                    return lines
    raise AssertionError(f"never received {until!r}")


def test_log_stream_truncates_endless_lines(api, auth, monkeypatch):
    big = [b"A" * 65536 for _ in range(64)]  # 4 MiB without a newline
    container = _FakeContainer([b"first\n", *big, b"\nlast\n"])
    monkeypatch.setattr(docker_ops, "get_container", lambda _iid: container)
    lines = _collect_lines(api, auth, uuid.uuid4(), until="last")
    assert lines[0] == "first"
    assert len(lines) == 3
    assert len(lines[1].encode()) <= 64 * 1024  # not the whole 4 MiB
    assert lines[1].endswith(main.LOG_TRUNCATED_SUFFIX)
    assert len(lines[1].encode()) <= main.LOG_LINE_MAX_BYTES + len(main.LOG_TRUNCATED_SUFFIX)


def test_log_stream_keeps_order_and_accounts_for_dropped_lines(api, auth, monkeypatch):
    monkeypatch.setattr(main, "LOG_BUFFER_MAX_LINES", 50, raising=False)
    n = 20000
    blob = b"".join(b"line %d\n" % i for i in range(n))
    container = _FakeContainer([blob, b"done\n"])
    monkeypatch.setattr(docker_ops, "get_container", lambda _iid: container)
    lines = _collect_lines(api, auth, uuid.uuid4(), until="done", limit=n + 100)
    dropped = 0
    numbers = []
    for line in lines[:-1]:
        if line.startswith("[mcpanel] "):
            dropped += int(line.split()[1])
        else:
            numbers.append(int(line.split()[1]))
    assert numbers == sorted(numbers)  # still in order
    assert len(numbers) + dropped == n  # every line was either delivered or counted as dropped


class _CountingLoop:
    def __init__(self, loop):  # type: ignore[no-untyped-def]
        self.loop = loop
        self.scheduled = 0

    def call_soon_threadsafe(self, *args):  # type: ignore[no-untyped-def]
        self.scheduled += 1
        return self.loop.call_soon_threadsafe(*args)


def test_log_buffer_is_bounded_and_drops_oldest():
    async def scenario() -> tuple[int, list[dict], int]:
        loop = _CountingLoop(asyncio.get_running_loop())
        buf = main._LogBuffer(loop, max_lines=100, max_bytes=10_000_000)  # type: ignore[arg-type]

        def produce() -> None:  # a log flood while the client reads nothing
            buf.put({"type": "state", "state": "running"})
            for i in range(50_000):
                buf.put({"type": "log", "line": f"l{i}"})

        await asyncio.to_thread(produce)
        size = len(buf)
        out: list[dict] = []
        while len(buf):
            out.extend(await buf.get_batch(1) or [])
        return size, out, loop.scheduled

    size, out, scheduled = asyncio.run(scenario())
    assert size <= 101  # 100 lines + the state message
    assert scheduled == 1  # one wake-up scheduled, not one event-loop callback per line
    assert out[0] == {"type": "state", "state": "running"}  # control messages are never dropped
    assert out[1]["line"].startswith("[mcpanel] 49900 console line(s) dropped")
    assert [m["line"] for m in out[2:]] == [f"l{i}" for i in range(49_900, 50_000)]  # newest kept


def test_log_buffer_respects_byte_limit():
    async def scenario() -> int:
        buf = main._LogBuffer(asyncio.get_running_loop(), max_lines=10_000, max_bytes=100_000)
        for _ in range(1000):
            buf.put({"type": "log", "line": "x" * 1000})
        return buf._bytes

    assert asyncio.run(scenario()) <= 100_000


def test_line_splitter_never_buffers_more_than_a_line():
    sp = main._LineSplitter(max_bytes=1000)
    out = []
    for _ in range(1000):
        out += sp.feed(b"z" * 4096)
        assert len(sp._buf) <= 1000
    out += sp.feed(b"tail\nok\npartial")
    out += sp.flush()
    assert out == ["z" * 1000 + main.LOG_TRUNCATED_SUFFIX, "ok", "partial"]


# =====================================================================================
# 5. Archive limits are enforced while the index is read
# =====================================================================================
@pytest.fixture
def aroot(tmp_path):
    inst = tmp_path / "inst"
    inst.mkdir()
    return SafeRoot(inst), inst


def _tar(path: Path, n: int, mode: str = "w") -> None:
    with tarfile.open(path, mode) as tf:
        for i in range(n):
            info = tarfile.TarInfo(f"f{i}.txt")
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))


def test_tar_entry_limit_is_enforced_while_reading_headers(aroot, monkeypatch):
    sr, inst = aroot
    _tar(inst / "many.tar", 400)
    monkeypatch.setattr(archive, "MAX_FILES", 20)
    reads = []
    original = tarfile.TarInfo.fromtarfile.__func__

    def counting(cls, tf):  # type: ignore[no-untyped-def]
        reads.append(1)
        return original(cls, tf)

    monkeypatch.setattr(tarfile.TarInfo, "fromtarfile", classmethod(counting))
    with pytest.raises(AgentError) as exc:
        archive.extract(sr, "many.tar", "out")
    assert exc.value.code == "archive_too_large"
    # stopped right after the limit (plus one probe per compression format tarfile tries first),
    # not after reading all 400 headers
    assert len(reads) <= 30
    assert not (inst / "out").exists()


def test_tar_oversized_pax_header_is_refused(aroot):
    sr, inst = aroot
    with tarfile.open(inst / "bomb.tar.gz", "w:gz", format=tarfile.PAX_FORMAT) as tf:
        info = tarfile.TarInfo("a.txt")
        info.size = 1
        info.pax_headers = {"comment": "c" * (4 * 1024 * 1024)}  # compresses to a few KB
        tf.addfile(info, io.BytesIO(b"x"))
    assert (inst / "bomb.tar.gz").stat().st_size < 100_000
    with pytest.raises(AgentError) as exc:
        archive.extract(sr, "bomb.tar.gz")
    assert exc.value.code == "archive_too_large"


def test_tar_extended_headers_are_bounded_in_total(aroot, monkeypatch):
    sr, inst = aroot
    monkeypatch.setattr(archive, "MAX_TAR_EXT_HEADERS_TOTAL", 2 * 1024 * 1024, raising=False)
    with tarfile.open(inst / "many-pax.tar.gz", "w:gz", format=tarfile.PAX_FORMAT) as tf:
        for i in range(10):
            info = tarfile.TarInfo(f"f{i}.txt")
            info.size = 1
            info.pax_headers = {"comment": "c" * 400_000}
            tf.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(AgentError) as exc:
        archive.extract(sr, "many-pax.tar.gz")
    assert exc.value.code == "archive_too_large"


def test_tar_gnu_sparse_member_is_refused(aroot):
    sr, inst = aroot
    info = tarfile.TarInfo("sparse.bin")
    info.type = tarfile.GNUTYPE_SPARSE
    info.size = 0
    data = info.tobuf(format=tarfile.GNU_FORMAT) + b"\0" * 1024
    (inst / "sparse.tar").write_bytes(data)
    with pytest.raises(AgentError) as exc:
        archive.extract(sr, "sparse.tar")
    assert exc.value.code == "unsupported_archive"


def test_tar_auto_strip_with_directory_entries(aroot):
    sr, inst = aroot
    with tarfile.open(inst / "pack.tar.gz", "w:gz") as tf:
        d = tarfile.TarInfo("server")
        d.type = tarfile.DIRTYPE
        tf.addfile(d)
        info = tarfile.TarInfo("server/server.properties")
        info.size = 3
        tf.addfile(info, io.BytesIO(b"a=b"))
    result = archive.extract(sr, "pack.tar.gz")
    assert result["stripped_prefix"] == "server"
    assert (inst / "server.properties").read_bytes() == b"a=b"


def _zip_bytes(n: int, *, comment: bytes = b"") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(n):
            zf.writestr(f"d/f{i}.txt", "x")
        zf.comment = comment
    return buf.getvalue()


@pytest.mark.parametrize("variant", ["plain", "comment"])
def test_zip_entry_limit_checked_before_zipfile_loads_the_directory(aroot, monkeypatch, variant):
    sr, inst = aroot
    data = _zip_bytes(60, comment=b"hello" if variant == "comment" else b"")
    (inst / "many.zip").write_bytes(data)
    monkeypatch.setattr(archive, "MAX_FILES", 50)
    opened = []
    real_zipfile = zipfile.ZipFile

    class CountingZipFile(real_zipfile):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):  # type: ignore[no-untyped-def]
            opened.append(1)
            super().__init__(*a, **kw)

    monkeypatch.setattr(zipfile, "ZipFile", CountingZipFile)
    with pytest.raises(AgentError) as exc:
        archive.extract(sr, "many.zip")
    assert exc.value.code == "archive_too_large"
    assert opened == []  # refused from a streaming count, before zipfile parsed anything


def test_zip_central_directory_size_is_bounded(aroot, monkeypatch):
    sr, inst = aroot
    (inst / "big-index.zip").write_bytes(_zip_bytes(60))
    monkeypatch.setattr(archive, "MAX_ZIP_DIRECTORY_BYTES", 1000, raising=False)
    with pytest.raises(AgentError) as exc:
        archive.extract(sr, "big-index.zip")
    assert exc.value.code == "archive_too_large"


def test_zip_directory_check_handles_zip64_and_accepts_valid_archives(monkeypatch):
    data = _zip_bytes(66_000)  # more than 65535 entries: zip64 end-of-directory records
    assert b"PK\x06\x06" in data[-200:]
    archive._check_zip_directory(io.BytesIO(data))  # within limits: accepted
    monkeypatch.setattr(archive, "MAX_FILES", 65_999)
    with pytest.raises(AgentError):
        archive._check_zip_directory(io.BytesIO(data))
    for variant in (_zip_bytes(3, comment=b"c" * 1000), b"junk" * 100 + _zip_bytes(3)):
        archive._check_zip_directory(io.BytesIO(variant))
        assert len(zipfile.ZipFile(io.BytesIO(variant)).infolist()) == 3


def test_extract_reports_are_bounded(aroot):
    sr, inst = aroot
    with zipfile.ZipFile(inst / "existing.zip", "w") as zf:
        for i in range(300):
            zf.writestr(f"f{i}.txt", "x")
    for i in range(300):
        (inst / f"f{i}.txt").write_text("keep")
    result = archive.extract(sr, "existing.zip", strip="none")
    assert result["existing_count"] == 300 and len(result["existing"]) == archive.REPORT_LIMIT


# =====================================================================================
# 6. An abandoned folder-zip download stops its producer thread
# =====================================================================================
def _zip_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "zip-stream"]


def test_closing_a_zip_stream_stops_the_producer(aroot, monkeypatch):
    sr, inst = aroot
    monkeypatch.setattr(archive, "ZIP_QUEUE_CHUNKS", 2, raising=False)
    monkeypatch.setattr(archive, "ZIP_PUT_POLL_SECONDS", 0.05, raising=False)
    (inst / "world").mkdir()
    for i in range(80):  # well over the queue size, even the old 64-chunk one
        (inst / "world" / f"r{i}.mca").write_bytes(os.urandom(64 * 1024))  # incompressible
    before = set(_zip_threads())
    gen = archive.stream_zip(sr, "world")
    assert next(gen)
    producers = [t for t in _zip_threads() if t not in before]
    assert len(producers) == 1
    gen.close()  # the HTTP client went away
    producers[0].join(5)
    assert not producers[0].is_alive(), "zip producer thread leaked"


def test_stalled_zip_consumer_stops_the_producer(aroot, monkeypatch):
    sr, inst = aroot
    monkeypatch.setattr(archive, "ZIP_QUEUE_CHUNKS", 2, raising=False)
    monkeypatch.setattr(archive, "ZIP_PUT_POLL_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(archive, "ZIP_STALL_SECONDS", 0.3, raising=False)
    (inst / "world").mkdir()
    for i in range(80):
        (inst / "world" / f"r{i}.mca").write_bytes(os.urandom(64 * 1024))
    before = set(_zip_threads())
    gen = archive.stream_zip(sr, "world")
    assert next(gen)  # ... and then never read again, never closed
    producers = [t for t in _zip_threads() if t not in before]
    producers[0].join(5)
    assert not producers[0].is_alive()
    del gen


def test_too_large_folder_is_refused_before_streaming(api, auth, agent_settings, monkeypatch):
    import functools

    iid = uuid.uuid4()
    (agent_settings.instances_dir / str(iid) / "world").mkdir(parents=True)
    (agent_settings.instances_dir / str(iid) / "world" / "level.dat").write_bytes(b"x" * 100)
    monkeypatch.setattr(archive, "stream_zip", functools.partial(archive.stream_zip, max_bytes=10))
    r = api.get(f"/v1/instances/{iid}/files/archive?path=world", headers=auth)
    assert r.status_code == 413 and r.json()["error"]["code"] == "archive_too_large"


# =====================================================================================
# 7. WebSockets from instance networks are refused too
# =====================================================================================
def test_websocket_from_instance_network_is_refused(monkeypatch):
    import ipaddress

    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr(main, "_blocked_networks", [ipaddress.ip_network("172.30.0.0/16")])
    monkeypatch.setenv("AGENT_TOKEN", TOKEN)
    monkeypatch.setattr(docker_ops, "get_container", lambda _iid: None)
    get_settings.cache_clear()
    try:
        client = TestClient(main.app, client=("172.30.0.7", 50000))
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                f"/v1/instances/{uuid.uuid4()}/logs/stream", headers={"Authorization": f"Bearer {TOKEN}"}
            ) as ws:
                ws.receive_json()
        assert exc.value.code == 4403
        # the same request from another network is accepted
        ok = TestClient(main.app, client=("10.1.2.3", 50000))
        with ok.websocket_connect(
            f"/v1/instances/{uuid.uuid4()}/logs/stream", headers={"Authorization": f"Bearer {TOKEN}"}
        ) as ws:
            assert ws.receive_json() == {"type": "state", "state": "missing"}
    finally:
        get_settings.cache_clear()


# =====================================================================================
# 8. restic output pipes cannot deadlock, and restic runs are time-limited
# =====================================================================================
FAKE_RESTIC = """#!{python}
import json, os, signal, sys, time
signal.alarm(30)  # never outlive a broken test
open({pidfile!r}, "w").write(str(os.getpid()))
cmd = sys.argv[1]
if cmd == "snapshots":
    print(json.dumps([{{"id": "abcdef0123456789", "short_id": "abcdef01", "paths": ["/x"]}}]))
    sys.exit(0)
mode = {mode!r}
if mode == "hang":
    time.sleep(60)
for i in range(20000):  # ~1.5 MB on stderr, far beyond the pipe buffer
    sys.stderr.write("error: lstat /data/world/vanished-file-%06d: no such file or directory\\n" % i)
sys.stderr.flush()
print(json.dumps({{"message_type": "summary", "snapshot_id": "0123456789abcdef"}}))
"""


def _fake_restic(tmp_path: Path, mode: str) -> Settings:
    binary = tmp_path / "fake-restic"
    binary.write_text(FAKE_RESTIC.format(python=sys.executable, mode=mode, pidfile=str(tmp_path / "pid")))
    binary.chmod(0o755)
    settings = Settings(data_root=tmp_path / "data", restic_password="pw", restic_binary=str(binary))
    settings.state_dir.mkdir(parents=True)
    return settings


@pytest.mark.parametrize("action", ["backup", "restore"])
def test_restic_stderr_flood_does_not_deadlock(tmp_path, clean_env, action):
    from mcagent.backups import Restic

    settings = _fake_restic(tmp_path, "flood")
    iid = uuid.uuid4()
    r = Restic(settings, iid)
    r.repo.mkdir(parents=True)
    (r.repo / "config").write_text("x")  # "initialized"
    if action == "backup":
        call = lambda: r.backup(tmp_path, ["t"], [])  # noqa: E731
    else:
        call = lambda: r.restore("abcdef01", tmp_path / "target")  # noqa: E731
    finished, result, exc = _run_with_deadline(call, 20)
    assert finished, f"restic {action} deadlocked on a full stderr pipe"
    assert exc is None, exc
    assert result.get("snapshot_id") == "0123456789abcdef"


def test_restic_backup_is_killed_after_its_timeout(tmp_path, clean_env, monkeypatch):
    from mcagent.backups import Restic

    settings = _fake_restic(tmp_path, "hang")
    monkeypatch.setattr(Restic, "BACKUP_TIMEOUT", 1, raising=False)
    r = Restic(settings, uuid.uuid4())
    r.repo.mkdir(parents=True)
    (r.repo / "config").write_text("x")
    started = time.monotonic()
    finished, _result, exc = _run_with_deadline(lambda: r.backup(tmp_path, [], []), 15)
    assert finished, "a hung restic was not stopped"
    assert isinstance(exc, AgentError) and exc.code == "restic_timeout"
    assert time.monotonic() - started < 10
    with pytest.raises(ProcessLookupError):  # the process was killed and reaped, not orphaned
        os.kill(int((tmp_path / "pid").read_text()), 0)


# =====================================================================================
# 9 + 10. Restores keep working on (and keep) btrfs subvolumes
# =====================================================================================
needs_restic = pytest.mark.skipif(shutil.which("restic") is None, reason="restic binary not installed")
BTRFS = os.environ.get("MCPANEL_TEST_BTRFS")
needs_btrfs = pytest.mark.skipif(
    not BTRFS or not shutil.which("btrfs"), reason="needs MCPANEL_TEST_BTRFS btrfs mount"
)


def _client_for(settings: Settings, monkeypatch):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    settings.token = TOKEN
    monkeypatch.setattr(main, "_blocked_networks", [])
    monkeypatch.setattr(docker_ops, "status", lambda _iid: {"running": False})
    main.app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(main.app), {"Authorization": f"Bearer {TOKEN}"}


def _make_server(settings: Settings, iid: uuid.UUID) -> tuple[Path, str]:
    from mcagent.backups import Restic
    from mcagent.snapshots import ensure_instance_dir

    for d in (settings.instances_dir, settings.backups_dir, settings.staging_dir, settings.trash_dir):
        d.mkdir(parents=True, exist_ok=True)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    inst = settings.instances_dir / str(iid)
    ensure_instance_dir(settings, inst)
    (inst / "world" / "region").mkdir(parents=True)
    (inst / "world" / "level.dat").write_bytes(b"LEVEL-v1")
    (inst / "world" / "region" / "r.0.0.mca").write_bytes(b"\x01" * 4096)
    (inst / "server.properties").write_text("motd=v1\n")
    snap = Restic(settings, iid).backup(inst, [f"instance:{iid}"], [])["snapshot_id"]
    (inst / "world" / "level.dat").write_bytes(b"LEVEL-broken")
    (inst / "world" / "region" / "r.0.0.mca").unlink()
    (inst / "server.properties").write_text("motd=v2\n")
    (inst / "added-later.txt").write_text("new")
    return inst, snap


@pytest.fixture
def btrfs_settings(clean_env):
    from mcagent.snapshots import is_subvolume, remove_tree

    base = Path(BTRFS or "/nonexistent") / f"t-audit-{uuid.uuid4().hex[:8]}"
    settings = Settings(data_root=base / "data", restic_password="test-password-123")
    yield settings
    main.app.dependency_overrides.pop(get_settings, None)
    for p in sorted(base.rglob("*"), key=lambda x: -len(str(x))):
        if p.is_dir() and not p.is_symlink() and is_subvolume(p):
            remove_tree(p)  # also without CAP_SYS_ADMIN, where `btrfs subvolume delete` is refused
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def plain_settings(tmp_path, clean_env):
    yield Settings(data_root=tmp_path / "data", restic_password="test-password-123")
    main.app.dependency_overrides.pop(get_settings, None)


def _leftovers(inst: Path) -> list[str]:
    found = [p.name for p in inst.rglob(".mcpanel-*")]
    found += [p.name for p in inst.parent.iterdir() if ".mcpanel-" in p.name]
    return found


def _restore_path(client, auth, iid, snap, path) -> dict:  # type: ignore[no-untyped-def]
    r = client.post(f"/v1/instances/{iid}/backups/{snap}/restore-path", json={"path": path}, headers=auth)
    assert r.status_code == 200, r.text
    return _wait_op(client, auth, r.json()["operation"]["id"])


def _check_restore_path(settings: Settings, monkeypatch) -> Path:  # type: ignore[no-untyped-def]
    iid = uuid.uuid4()
    inst, snap = _make_server(settings, iid)
    client, auth = _client_for(settings, monkeypatch)
    op = _restore_path(client, auth, iid, snap, "world")  # a folder
    assert op["status"] == "succeeded", op
    assert (inst / "world" / "level.dat").read_bytes() == b"LEVEL-v1"
    assert (inst / "world" / "region" / "r.0.0.mca").read_bytes() == b"\x01" * 4096
    op = _restore_path(client, auth, iid, snap, "server.properties")  # a single file
    assert op["status"] == "succeeded", op
    assert (inst / "server.properties").read_text() == "motd=v1\n"
    assert (inst / "added-later.txt").read_text() == "new"  # nothing else touched
    (inst / "world" / "region" / "r.0.0.mca").unlink()
    (inst / "world" / "region").rmdir()
    op = _restore_path(client, auth, iid, snap, "world/region/r.0.0.mca")  # parent re-created
    assert op["status"] == "succeeded", op
    assert (inst / "world" / "region" / "r.0.0.mca").exists()
    op = _restore_path(client, auth, iid, snap, "missing.txt")
    assert op["status"] == "failed" and op["error_code"] == "not_found"
    op = _restore_path(client, auth, iid, snap, "nope/deeper/missing.txt")
    assert op["status"] == "failed"  # restic refuses the missing parent folder
    assert not (inst / "nope").exists()  # a failed restore creates no folders
    assert _leftovers(inst) == []
    return inst


@needs_restic
def test_restore_path_plain_directory(plain_settings, monkeypatch):
    _check_restore_path(plain_settings, monkeypatch)


@needs_restic
@needs_btrfs
def test_restore_path_into_btrfs_subvolume(btrfs_settings, monkeypatch):
    from mcagent.snapshots import is_subvolume

    inst = _check_restore_path(btrfs_settings, monkeypatch)
    assert is_subvolume(inst)


def _full_restore(client, auth, iid, snap, target=None) -> dict:  # type: ignore[no-untyped-def]
    body = {"snapshot_id": snap, **({"target_instance_id": str(target)} if target else {})}
    r = client.post(f"/v1/instances/{iid}/backups/restore", json=body, headers=auth)
    assert r.status_code == 200, r.text
    return _wait_op(client, auth, r.json()["operation"]["id"])


def _assert_restored(inst: Path) -> None:
    assert (inst / "world" / "level.dat").read_bytes() == b"LEVEL-v1"
    assert (inst / "world" / "region" / "r.0.0.mca").read_bytes() == b"\x01" * 4096
    assert (inst / "server.properties").read_text() == "motd=v1\n"
    assert not (inst / "added-later.txt").exists()


@needs_restic
@needs_btrfs
def test_full_restore_keeps_the_instance_a_subvolume(btrfs_settings, monkeypatch):
    from mcagent.snapshots import BtrfsSnapshots, is_subvolume

    iid = uuid.uuid4()
    inst, snap = _make_server(btrfs_settings, iid)
    assert is_subvolume(inst)
    snaps = BtrfsSnapshots(btrfs_settings, iid)
    before = snaps.create("before")
    client, auth = _client_for(btrfs_settings, monkeypatch)
    op = _full_restore(client, auth, iid, "deadbeef")  # fails after the staging subvolume exists
    assert op["status"] == "failed" and op["error_code"] == "snapshot_not_found"
    assert _leftovers(inst) == [] and is_subvolume(inst)
    op = _full_restore(client, auth, iid, snap)
    assert op["status"] == "succeeded", op
    assert "warnings" not in op["result"]
    _assert_restored(inst)
    assert is_subvolume(inst), "a full restore turned the subvolume into a plain directory"
    assert _leftovers(inst) == []
    # snapshots keep working: an older snapshot and a new one
    assert snaps.create("after")["name"]
    snaps.restore(before["name"])
    assert (inst / "world" / "level.dat").read_bytes() == b"LEVEL-broken"


@needs_restic
@needs_btrfs
def test_full_restore_into_new_instance_on_btrfs_is_a_subvolume(btrfs_settings, monkeypatch):
    from mcagent.snapshots import is_subvolume

    iid, target = uuid.uuid4(), uuid.uuid4()
    _inst, snap = _make_server(btrfs_settings, iid)
    client, auth = _client_for(btrfs_settings, monkeypatch)
    op = _full_restore(client, auth, iid, snap, target)
    assert op["status"] == "succeeded", op
    out = btrfs_settings.instances_dir / str(target)
    _assert_restored(out)
    assert is_subvolume(out)


@needs_restic
def test_full_restore_plain_directory(plain_settings, monkeypatch):
    iid = uuid.uuid4()
    inst, snap = _make_server(plain_settings, iid)
    client, auth = _client_for(plain_settings, monkeypatch)
    chowned: list[tuple[Path, int, int]] = []
    real_chown = os.chown

    def spy(path, uid, gid, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not isinstance(path, int):
            chowned.append((Path(path), uid, gid))
        return real_chown(path, uid, gid, *args, **kwargs)

    monkeypatch.setattr(os, "chown", spy)
    op = _full_restore(client, auth, iid, snap)
    assert op["status"] == "succeeded", op
    _assert_restored(inst)
    # the server folder belongs to the runtime uid/gid again (restic made the staging folder as root):
    # a rootless image (Factorio) must be able to create files at its top level
    assert (inst, plain_settings.runtime_uid, plain_settings.runtime_gid) in chowned
    assert list(plain_settings.staging_dir.iterdir()) == []
    assert list(plain_settings.trash_dir.iterdir()) == []


@needs_btrfs
@pytest.mark.parametrize("prepared", [False, True])
def test_clone_on_btrfs_is_a_subvolume_and_skips_special_files(btrfs_settings, monkeypatch, prepared):
    """A clone gets a subvolume like any new server (built next to it, renamed into place)."""
    from mcagent.snapshots import ensure_instance_dir, is_subvolume

    for d in (btrfs_settings.instances_dir, btrfs_settings.staging_dir, btrfs_settings.trash_dir):
        d.mkdir(parents=True, exist_ok=True)
    btrfs_settings.state_dir.mkdir(parents=True, exist_ok=True)
    src, dst = uuid.uuid4(), uuid.uuid4()
    inst = btrfs_settings.instances_dir / str(src)
    ensure_instance_dir(btrfs_settings, inst)
    (inst / "world").mkdir()
    (inst / "world" / "level.dat").write_bytes(b"L")
    os.mkfifo(inst / "pipe")
    os.symlink("world", inst / "link")
    out = btrfs_settings.instances_dir / str(dst)
    if prepared:  # /prepare already made the target folder (an empty subvolume)
        assert ensure_instance_dir(btrfs_settings, out) and is_subvolume(out)
    client, auth = _client_for(btrfs_settings, monkeypatch)
    r = client.post(f"/v1/instances/{src}/clone", json={"target_instance_id": str(dst)}, headers=auth)
    op = _wait_op(client, auth, r.json()["operation"]["id"])
    assert op["status"] == "succeeded", op
    assert is_subvolume(out), "the clone is a plain directory (snapshots would be off)"
    assert (out / "world" / "level.dat").read_bytes() == b"L"
    assert os.path.islink(out / "link") and not os.path.lexists(out / "pipe")
    assert _leftovers(out) == [] and "warnings" not in op["result"]
