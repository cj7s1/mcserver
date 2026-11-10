"""GET /v1/instances/{id}/ping: Minecraft server list ping, against a small in-process fake server."""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
import uuid

import pytest

from mcagent import docker_ops, main, slp

from . import test_lastrun
from .test_lastrun import IID, FakeContainer

world = test_lastrun.world  # the fixture


def _read_varint(sock: socket.socket) -> int:
    value = 0
    for i in range(5):
        byte = sock.recv(1)
        if not byte:
            raise EOFError
        value |= (byte[0] & 0x7F) << (7 * i)
        if not byte[0] & 0x80:
            return value
    raise ValueError("varint too long")


def _read_packet(sock: socket.socket) -> bytes:
    length = _read_varint(sock)
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


class FakeServer:
    """Answers one status request per connection like a Minecraft server (or misbehaves)."""

    def __init__(self, status: dict | None = None, behaviour: str = "ok") -> None:
        self.status = status or {}
        self.behaviour = behaviour
        self.handshakes: list[dict] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    self._one(conn)
                except (EOFError, OSError):
                    pass

    def _one(self, conn: socket.socket) -> None:
        handshake = _read_packet(conn)
        pid, pos = slp._decode_varint(handshake, 0)
        protocol, pos = slp._decode_varint(handshake, pos)
        n, pos = slp._decode_varint(handshake, pos)
        host = handshake[pos : pos + n].decode()
        pos += n
        (port,) = struct.unpack(">H", handshake[pos : pos + 2])
        next_state, _ = slp._decode_varint(handshake, pos + 2)
        request = _read_packet(conn)
        self.handshakes.append(
            {
                "id": pid,
                "protocol": protocol,
                "host": host,
                "port": port,
                "next": next_state,
                "request": request,
            }
        )
        if self.behaviour == "silent":
            time.sleep(10)
            return
        if self.behaviour == "garbage":
            conn.sendall(b"\x05\x7fjunk")
            return
        body = json.dumps(self.status).encode()
        conn.sendall(slp.packet(0x00, slp.varint(len(body)) + body))
        ping = _read_packet(conn)
        if self.behaviour == "no_pong":
            return
        conn.sendall(slp.varint(len(ping)) + ping)  # the pong echoes the payload

    def close(self) -> None:
        self.sock.close()


STATUS = {
    "version": {"name": "Paper 1.21.10", "protocol": 773},
    "players": {
        "max": 20,
        "online": 2,
        "sample": [{"name": "Steve", "id": str(uuid.uuid4())}, {"name": "§aAlex", "id": str(uuid.uuid4())}],
    },
    "description": {"text": "§6Bruh ", "extra": [{"text": "server", "bold": True}, "!"]},
    "favicon": "data:image/png;base64," + "A" * 5000,
}


@pytest.fixture
def server(monkeypatch):
    servers: list[FakeServer] = []

    def make(status: dict | None = None, behaviour: str = "ok") -> FakeServer:
        s = FakeServer(status, behaviour)
        servers.append(s)
        monkeypatch.setattr(docker_ops, "game_endpoint", lambda _settings, _iid: ("127.0.0.1", s.port))
        return s

    yield make
    for s in servers:
        s.close()


def test_ping_reports_players_version_and_motd(api, auth, server):
    s = server(STATUS)
    r = api.get(f"/v1/instances/{IID}/ping", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {k: body[k] for k in ("online", "max", "sample", "version", "motd")} == {
        "online": 2,
        "max": 20,
        "sample": ["Steve", "Alex"],
        "version": "Paper 1.21.10",
        "motd": "Bruh server!",
    }
    assert isinstance(body["latency_ms"], float) and 0 <= body["latency_ms"] < 5000
    (hs,) = s.handshakes
    assert hs == {"id": 0, "protocol": -1, "host": "127.0.0.1", "port": s.port, "next": 1, "request": b"\x00"}


def test_ping_of_a_server_that_does_not_answer_pongs_still_reports(api, auth, server):
    server({"players": {"online": 0, "max": 5}, "description": "plain motd"}, behaviour="no_pong")
    body = api.get(f"/v1/instances/{IID}/ping", headers=auth).json()
    assert body["online"] == 0 and body["max"] == 5 and body["motd"] == "plain motd" and body["sample"] == []
    assert body["version"] == "" and body["latency_ms"] >= 0


def test_ping_is_bounded_in_time(api, auth, server, monkeypatch):
    server(behaviour="silent")
    monkeypatch.setattr(main, "PING_TIMEOUT", 0.5)
    started = time.monotonic()
    r = api.get(f"/v1/instances/{IID}/ping", headers=auth)
    assert r.status_code == 504 and r.json()["error"]["code"] == "ping_timeout"
    assert time.monotonic() - started < 3


def test_ping_of_something_that_is_not_a_minecraft_server(api, auth, server):
    server(behaviour="garbage")
    r = api.get(f"/v1/instances/{IID}/ping", headers=auth)
    assert r.status_code == 502 and r.json()["error"]["code"] == "ping_failed"


def test_ping_of_a_closed_port(api, auth, monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens there
    monkeypatch.setattr(docker_ops, "game_endpoint", lambda _settings, _iid: ("127.0.0.1", port))
    r = api.get(f"/v1/instances/{IID}/ping", headers=auth)
    assert r.status_code == 502 and r.json()["error"]["code"] == "ping_failed"


@pytest.mark.parametrize("state", [None, "exited", "created"])
def test_ping_without_a_running_container_is_409_not_running(api, auth, world, state):
    if state is None:
        world.container = None
    else:
        world.container.state = state
    r = api.get(f"/v1/instances/{IID}/ping", headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_running"


def test_the_ping_goes_to_the_container_on_the_instance_network(world, agent_settings):
    world.container = FakeContainer(world.calls, status="running")
    assert docker_ops.game_endpoint(agent_settings, IID) == ("172.30.0.5", docker_ops.GAME_PORT)


def test_status_parsing_is_bounded_and_defensive():
    huge = {
        "players": {"online": True, "max": -3, "sample": [{"name": "x" * 500}] * 500 + ["bad"]},
        "version": {"name": 7},
        "description": [{"text": "a"}, {"translate": "b", "extra": [{"text": "§lc"}]}],
    }
    parsed = slp.parse_status(json.dumps(huge))
    assert parsed["online"] == 0 and parsed["max"] == 0 and parsed["version"] == ""
    assert len(parsed["sample"]) == slp.MAX_SAMPLE and len(parsed["sample"][0]) == slp.MAX_NAME
    assert parsed["motd"] == "abc"
    with pytest.raises(slp.SlpError):
        slp.parse_status("[1, 2]")
    with pytest.raises(slp.SlpError):
        slp.parse_status("not json")


@pytest.mark.parametrize("value", [0, 1, 127, 128, 255, 25565, 2**31 - 1, -1])
def test_varint_round_trip(value):
    encoded = slp.varint(value)
    assert len(encoded) <= 5 and slp._decode_varint(encoded, 0) == (value, len(encoded))
