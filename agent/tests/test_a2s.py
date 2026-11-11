"""A2S (Valve's server query) client and the ``a2s`` health kind, against a fake UDP server."""

from __future__ import annotations

import socket
import struct
import threading
import time
from types import SimpleNamespace

import pytest

from mcagent import a2s, docker_ops
from mcagent.profiles import cs2, rust


def info_payload(name="Dust Club", players=3, max_players=12, edf: bytes = b"") -> bytes:
    return (
        b"\xff\xff\xff\xffI"
        + bytes([17])
        + name.encode()
        + b"\x00de_dust2\x00csgo\x00Counter-Strike 2\x00"
        + struct.pack("<H", 730)
        + bytes([players, max_players, 1])
        + b"dl"
        + bytes([0, 1])
        + b"1.41.9.2\x00"
        + edf
    )


def players_payload(names) -> bytes:
    body = bytes([len(names)])
    for i, n in enumerate(names):
        body += bytes([i]) + n.encode() + b"\x00" + struct.pack("<i", 5 * i) + struct.pack("<f", 61.5)
    return b"\xff\xff\xff\xffD" + body


class FakeA2S:
    """Answers A2S_INFO / A2S_PLAYER; ``challenge`` makes it ask for one first; ``mode`` breaks it."""

    CHALLENGE = b"\x0a\x0b\x0c\x0d"

    def __init__(self, *, challenge=True, mode="ok", names=("alice", "", "bob")):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.challenge = challenge
        self.mode = mode
        self.names = names
        self.requests: list[bytes] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        self.sock.settimeout(0.1)
        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except TimeoutError:
                continue
            except OSError:
                return
            self.requests.append(data)
            reply = self.answer(data)
            if reply is not None:
                self.sock.sendto(reply, addr)

    def answer(self, data: bytes) -> bytes | None:
        if self.mode == "silent":
            return None
        if self.mode == "garbage":
            return b"hello, this is not A2S"
        if self.mode == "split":
            return b"\xff\xff\xff\xfe" + b"\x00" * 12
        if self.mode == "wrong-type":
            return b"\xff\xff\xff\xffX\x00"
        if self.mode == "endless-challenge":
            return b"\xff\xff\xff\xffA" + self.CHALLENGE
        if self.mode == "truncated":
            return info_payload()[:12]
        if data.startswith(b"\xff\xff\xff\xffTSource Engine Query\x00"):
            if self.challenge and not data.endswith(self.CHALLENGE):
                return b"\xff\xff\xff\xffA" + self.CHALLENGE
            return info_payload(edf=b"\x80" + struct.pack("<H", 27015) + b"")
        if data.startswith(b"\xff\xff\xff\xffU"):
            if data[5:9] != self.CHALLENGE:
                return b"\xff\xff\xff\xffA" + self.CHALLENGE
            return players_payload(self.names)
        return None

    def close(self):
        self.stop.set()
        self.sock.close()
        self.thread.join(1)


@pytest.fixture
def fake():
    servers = []

    def make(**kw):
        s = FakeA2S(**kw)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


@pytest.mark.parametrize("challenge", [True, False])
def test_info_with_and_without_challenge(fake, challenge):
    server = fake(challenge=challenge)
    data = a2s.info("127.0.0.1", server.port, timeout=1.0)
    assert data["name"] == "Dust Club"
    assert data["map"] == "de_dust2"
    assert (data["players"], data["max_players"], data["bots"]) == (3, 12, 1)
    assert data["version"] == "1.41.9.2"
    assert data["port"] == 27015
    assert data["latency_ms"] >= 0
    assert len(server.requests) == (2 if challenge else 1)
    if challenge:
        assert server.requests[1].endswith(FakeA2S.CHALLENGE)


def test_players_with_challenge(fake):
    server = fake()
    players = a2s.players("127.0.0.1", server.port, timeout=1.0)
    assert [p["name"] for p in players] == ["alice", "", "bob"]
    assert players[2]["score"] == 10 and players[0]["duration"] == 61.5
    assert server.requests[0] == b"\xff\xff\xff\xffU\xff\xff\xff\xff"  # the placeholder challenge first


def test_status_answers_like_the_server_list_ping(fake):
    server = fake()
    answer = a2s.status("127.0.0.1", server.port, timeout=2.0)
    assert answer["online"] == 3 and answer["max"] == 12
    assert answer["sample"] == ["alice", "bob"]  # players still connecting have no name
    assert answer["motd"] == "Dust Club" and answer["version"] == "1.41.9.2" and answer["map"] == "de_dust2"


@pytest.mark.parametrize("mode", ["garbage", "split", "wrong-type", "endless-challenge", "truncated"])
def test_bad_replies_are_errors(fake, mode):
    server = fake(mode=mode)
    with pytest.raises(a2s.A2SError):
        a2s.info("127.0.0.1", server.port, timeout=0.5)


def test_silence_is_a_timeout(fake):
    server = fake(mode="silent")
    started = time.monotonic()
    with pytest.raises(a2s.A2SError, match="in time"):
        a2s.info("127.0.0.1", server.port, timeout=0.3)
    assert time.monotonic() - started < 2


def test_nothing_listening_is_an_error():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(a2s.A2SError):
        a2s.info("127.0.0.1", port, timeout=0.3)


def test_parsers_refuse_short_data():
    with pytest.raises(a2s.A2SError):
        a2s.parse_info(b"\x11abc")  # a string without its end
    assert a2s.parse_players(b"\x05") == []  # a count but no players: what arrived


# --- the a2s health kind ------------------------------------------------------------------------------
class _Container:
    def __init__(self, env, labels, started="2026-11-11T10:00:00.000000000Z"):
        self.id = f"c-{time.monotonic_ns()}"
        self.name = "mcpanel-test"
        self.labels = labels
        self.attrs = {
            "Config": {"Env": [f"{k}={v}" for k, v in env.items()]},
            "NetworkSettings": {"Networks": {"mcpanel_instances": {"IPAddress": "127.0.0.1"}}},
            "HostConfig": {},
        }
        self.started = started


def _described(c):
    return {"running": True, "started_at": c.started}


@pytest.mark.parametrize(
    "profile,env_key", [(cs2.PROFILE, "CS2_PORT"), (rust.PROFILE, "RUST_SERVER_QUERYPORT")]
)
def test_health_reads_the_port_from_the_containers_variable(
    fake, agent_settings, monkeypatch, profile, env_key
):
    monkeypatch.setattr(docker_ops, "get_settings", lambda: agent_settings)
    server = fake()
    c = _Container({env_key: str(server.port)}, {"io.mcpanel.game": profile.game})
    health, output = docker_ops.agent_health(c, profile, _described(c), now=time.time())
    assert health == "healthy"
    assert f"A2S_INFO on UDP port {server.port}" in output


def test_health_is_starting_then_unhealthy_without_an_answer(fake, agent_settings, monkeypatch):
    monkeypatch.setattr(docker_ops, "get_settings", lambda: agent_settings)
    server = fake(mode="silent")
    monkeypatch.setattr(docker_ops, "HEALTH_UDP_TIMEOUT", 0.2)
    c = _Container({"CS2_PORT": str(server.port)}, {"io.mcpanel.game": "cs2"})
    started = docker_ops._timestamp(c.started).timestamp()
    assert docker_ops.agent_health(c, cs2.PROFILE, _described(c), now=started + 5)[0] == "starting"
    c2 = _Container({"CS2_PORT": str(server.port)}, {"io.mcpanel.game": "cs2"})
    late = started + cs2.PROFILE.health.start_period_s + 1
    assert docker_ops.agent_health(c2, cs2.PROFILE, _described(c2), now=late)[0] == "unhealthy"


def test_health_result_is_cached_for_a_few_seconds(fake, agent_settings, monkeypatch):
    monkeypatch.setattr(docker_ops, "get_settings", lambda: agent_settings)
    server = fake()
    c = _Container({"CS2_PORT": str(server.port)}, {"io.mcpanel.game": "cs2"})
    now = time.time()
    assert docker_ops.agent_health(c, cs2.PROFILE, _described(c), now=now)[0] == "healthy"
    asked = len(server.requests)
    assert docker_ops.agent_health(c, cs2.PROFILE, _described(c), now=now + 1)[0] == "healthy"
    assert len(server.requests) == asked  # within HEALTH_TCP_CACHE_SECONDS: not asked again


def test_env_port_falls_back_to_the_profile_port():
    c = SimpleNamespace(attrs={"Config": {"Env": ["CS2_PORT=not-a-port"]}})
    assert docker_ops.env_port(c, "CS2_PORT", 27015) == 27015
    c = SimpleNamespace(attrs={"Config": {"Env": ["CS2_PORT=70000"]}})
    assert docker_ops.env_port(c, "CS2_PORT", 27015) == 27015
    c = SimpleNamespace(attrs={"Config": {"Env": ["CS2_PORT=27040"]}})
    assert docker_ops.env_port(c, "CS2_PORT", 27015) == 27040
