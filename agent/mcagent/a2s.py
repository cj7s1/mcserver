"""Valve's server query (A2S) over UDP: ``A2S_INFO`` and ``A2S_PLAYER``, with the challenge handshake.

Used by the ``a2s`` health kind (``docker_ops.agent_health``) and by the status query (``GET /ping``)
of games that answer it: CS2 on its game port, Rust on its query port, Project Zomboid on its game
port, Valheim on game port + 1 when the server is public. Palworld does not answer it (probe).

Protocol (https://developer.valvesoftware.com/wiki/Server_queries): a request starts with four
0xFF bytes and a type (``T`` + "Source Engine Query\\0" for info, ``U`` + challenge for players).
A server may answer ``A`` + a 4-byte challenge instead; the request is then sent again with that
challenge appended (info) or in place of the 0xFFFFFFFF placeholder (players). Replies: ``I``
(info), ``D`` (players). A split reply (header 0xFFFFFFFE) is not supported (servers send info and
small player lists in one datagram); it counts as no answer.

The peer is a game server running third-party code: every reply is bounded (one datagram of at
most MAX_REPLY bytes), parsed defensively and refused as :class:`A2SError` when it is not what was
asked for. Every call is bounded in time (``timeout`` per datagram, at most MAX_CHALLENGES
handshakes).
"""

from __future__ import annotations

import socket
import struct
import time
from typing import Any

HEADER = b"\xff\xff\xff\xff"
INFO_REQUEST = HEADER + b"TSource Engine Query\x00"
PLAYER_REQUEST = HEADER + b"U"
NO_CHALLENGE = b"\xff\xff\xff\xff"
MAX_REPLY = 8192
MAX_CHALLENGES = 2
MAX_PLAYERS = 256


class A2SError(Exception):
    """No valid answer: timeout, refused, or a reply that is not A2S."""


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise A2SError("The A2S reply ended early")
        value = self.data[self.pos]
        self.pos += 1
        return value

    def unpack(self, fmt: str) -> Any:
        size = struct.calcsize(fmt)
        if self.pos + size > len(self.data):
            raise A2SError("The A2S reply ended early")
        (value,) = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += size
        return value

    def string(self, limit: int = 256) -> str:
        end = self.data.find(b"\x00", self.pos)
        if end < 0:
            raise A2SError("The A2S reply ended inside a string")
        raw = self.data[self.pos : end]
        self.pos = end + 1
        return raw[:limit].decode("utf-8", errors="replace")

    @property
    def left(self) -> int:
        return len(self.data) - self.pos


def _exchange(sock: socket.socket, payload: bytes, timeout: float) -> bytes:
    sock.settimeout(max(0.05, timeout))
    try:
        sock.send(payload)
        reply = sock.recv(MAX_REPLY)
    except TimeoutError as exc:
        raise A2SError("The server did not answer the A2S query in time") from exc
    except OSError as exc:
        raise A2SError(f"The A2S query failed: {exc}") from exc
    if len(reply) < 5:
        raise A2SError("The A2S reply is too short")
    if reply[:4] == b"\xff\xff\xff\xfe":
        raise A2SError("The server answered with a split A2S reply (not supported)")
    if reply[:4] != HEADER:
        raise A2SError("The reply is not an A2S reply")
    return reply


def _ask(host: str, port: int, request: Any, want: int, timeout: float) -> tuple[bytes, float]:
    """Send ``request(challenge)`` and follow up to MAX_CHALLENGES challenges until a reply of type
    ``want`` arrives; (its payload after the type byte, latency in ms of the last exchange)."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect((host, port))
        except OSError as exc:
            raise A2SError(f"The A2S query failed: {exc}") from exc
        challenge: bytes | None = None
        for _ in range(MAX_CHALLENGES + 1):
            started = time.monotonic()
            reply = _exchange(sock, request(challenge), timeout)
            latency = (time.monotonic() - started) * 1000
            kind = reply[4]
            if kind == want:
                return reply[5:], latency
            if kind == ord("A"):
                if len(reply) < 9:
                    raise A2SError("The A2S challenge is too short")
                challenge = reply[5:9]
                continue
            raise A2SError(f"Unexpected A2S reply type 0x{kind:02x}")
    raise A2SError("The server kept answering the A2S query with a new challenge")


def parse_info(payload: bytes) -> dict[str, Any]:
    """The fields of an ``I`` reply (after its type byte)."""
    r = _Reader(payload)
    out: dict[str, Any] = {"protocol": r.byte()}
    out["name"] = r.string()
    out["map"] = r.string()
    out["folder"] = r.string()
    out["game"] = r.string()
    out["app_id"] = r.unpack("<H")
    out["players"] = r.byte()
    out["max_players"] = r.byte()
    out["bots"] = r.byte()
    out["server_type"] = chr(r.byte())
    out["environment"] = chr(r.byte())
    out["visibility"] = r.byte()
    out["vac"] = r.byte()
    out["version"] = r.string(64)
    if r.left:
        edf = r.byte()
        if edf & 0x80:
            out["port"] = r.unpack("<H")
        if edf & 0x10:
            out["steam_id"] = r.unpack("<Q")
        if edf & 0x40:
            out["tv_port"] = r.unpack("<H")
            out["tv_name"] = r.string()
        if edf & 0x20:
            out["keywords"] = r.string(512)
        if edf & 0x01:
            out["game_id"] = r.unpack("<Q")
    return out


def parse_players(payload: bytes) -> list[dict[str, Any]]:
    """The players of a ``D`` reply (after its type byte): name, score, duration (seconds)."""
    r = _Reader(payload)
    count = r.byte()
    players: list[dict[str, Any]] = []
    for _ in range(min(count, MAX_PLAYERS)):
        if not r.left:
            break
        r.byte()  # index (always 0 on most servers)
        name = r.string(64)
        score = r.unpack("<i")
        duration = r.unpack("<f")
        players.append({"name": name, "score": score, "duration": round(float(duration), 1)})
    return players


def info(host: str, port: int, timeout: float = 1.0) -> dict[str, Any]:
    """A2S_INFO: name, map, folder, game, players, max_players, bots, version, ... and latency_ms."""
    payload, latency = _ask(host, port, lambda ch: INFO_REQUEST + (ch or b""), ord("I"), timeout)
    out = parse_info(payload)
    out["latency_ms"] = round(latency, 1)
    return out


def players(host: str, port: int, timeout: float = 1.0) -> list[dict[str, Any]]:
    """A2S_PLAYER: [{name, score, duration}] (players still connecting have an empty name)."""
    payload, _latency = _ask(host, port, lambda ch: PLAYER_REQUEST + (ch or NO_CHALLENGE), ord("D"), timeout)
    return parse_players(payload)


def status(host: str, port: int, timeout: float = 2.0) -> dict[str, Any]:
    """What ``GET /ping`` answers for a game queried by A2S (like the server list ping): ``online``,
    ``max``, ``sample`` (names of A2S_PLAYER, empty when the server does not list them),
    ``version``, ``motd`` (the server name), ``map``, ``bots`` and ``latency_ms``."""
    per = max(0.2, timeout / 2)
    data = info(host, port, per)
    try:
        names = [p["name"] for p in players(host, port, per) if p["name"]]
    except A2SError:
        names = []
    return {
        "online": data["players"],
        "max": data["max_players"],
        "sample": names,
        "version": data["version"],
        "motd": data["name"],
        "map": data["map"],
        "bots": data["bots"],
        "latency_ms": data["latency_ms"],
    }


__all__ = ["A2SError", "info", "parse_info", "parse_players", "players", "status"]
