"""Minecraft server list ping (the status protocol mc-health and the game's server list use).

Handshake (next state 1), status request, JSON status response, then ping/pong for the
latency. Everything the server sends is third-party data: lengths are bounded, the JSON is
size-limited and only the fields the panel needs are taken from it, as plain bounded values.
No legacy (pre-1.7) fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import struct
import time
from typing import Any

PROTOCOL_VERSION = -1  # "unknown": every server answers the status request for any version
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # modded servers list their mods; favicons are base64 PNGs
MAX_SAMPLE = 100
MAX_NAME = 64
MAX_MOTD = 1024
MAX_VERSION = 256
PONG_TIMEOUT = 1.0
_FORMATTING = re.compile(r"§.")


class SlpError(Exception):
    """The server's answer was not a valid status response."""


def varint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for i in range(5):
        if pos >= len(data):
            raise SlpError("Truncated VarInt")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            if value & 0x80000000:
                value -= 1 << 32
            return value, pos
    raise SlpError("VarInt is too long")


async def _read_varint(reader: asyncio.StreamReader) -> int:
    raw = bytearray()
    for _ in range(5):
        raw += await reader.readexactly(1)
        if not raw[-1] & 0x80:
            return _decode_varint(bytes(raw), 0)[0]
    raise SlpError("VarInt is too long")


def packet(packet_id: int, payload: bytes) -> bytes:
    body = varint(packet_id) + payload
    return varint(len(body)) + body


def _string(text: str) -> bytes:
    data = text.encode("utf-8")
    return varint(len(data)) + data


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes, int]:
    length = await _read_varint(reader)
    if length <= 0 or length > MAX_RESPONSE_BYTES:
        raise SlpError(f"Invalid packet length {length}")
    data = await reader.readexactly(length)
    packet_id, pos = _decode_varint(data, 0)
    return packet_id, data, pos


def _plain_text(component: Any, depth: int = 0) -> str:
    """The text of a chat component (string, object with text/extra, or list), without colours."""
    if depth > 32:
        return ""
    if isinstance(component, str):
        return component
    if isinstance(component, list):
        return "".join(_plain_text(c, depth + 1) for c in component[:256])
    if isinstance(component, dict):
        text = component.get("text")
        out = text if isinstance(text, str) else ""
        if not out and isinstance(component.get("translate"), str):
            out = component["translate"]
        extra = component.get("extra")
        if isinstance(extra, list):
            out += "".join(_plain_text(c, depth + 1) for c in extra[:256])
        return out
    return ""


# a player count above this is not believable (and would not fit the panel's integer column; C-1)
MAX_COUNT = 100_000


def _count(value: Any) -> int:
    """A player count the server reported: an int in 0..MAX_COUNT, anything else 0 (as before for a
    value that is not a count)."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_COUNT:
        return value
    return 0


def parse_status(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise SlpError(f"The status response is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SlpError("The status response is not a JSON object")
    players = data.get("players") if isinstance(data.get("players"), dict) else {}
    sample: list[str] = []
    raw_sample = players.get("sample")
    for entry in raw_sample if isinstance(raw_sample, list) else []:
        if len(sample) >= MAX_SAMPLE:
            break
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            sample.append(_FORMATTING.sub("", entry["name"])[:MAX_NAME])
    version = data.get("version") if isinstance(data.get("version"), dict) else {}
    name = version.get("name")
    motd = _FORMATTING.sub("", _plain_text(data.get("description")))
    return {
        "online": _count(players.get("online")),
        "max": _count(players.get("max")),
        "sample": sample,
        "version": (_FORMATTING.sub("", name) if isinstance(name, str) else "")[:MAX_VERSION],
        "motd": motd[:MAX_MOTD],
    }


async def ping(host: str, port: int, timeout: float = 5.0) -> dict[str, Any]:
    """Status of the server at ``host:port``: online, max, sample (names), version, motd, latency_ms.

    Raises ``TimeoutError`` after ``timeout`` seconds, ``OSError`` when the connection fails and
    :class:`SlpError` for an invalid answer.
    """
    deadline = time.monotonic() + timeout

    async def status() -> tuple[asyncio.StreamReader, asyncio.StreamWriter, dict[str, Any], float]:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            handshake = varint(PROTOCOL_VERSION) + _string(host) + struct.pack(">H", port) + varint(1)
            writer.write(packet(0x00, handshake) + packet(0x00, b""))
            await writer.drain()
            sent = time.monotonic()
            packet_id, data, pos = await _read_packet(reader)
            elapsed = time.monotonic() - sent
            if packet_id != 0x00:
                raise SlpError(f"Unexpected packet 0x{packet_id:02x} instead of the status response")
            length, pos = _decode_varint(data, pos)
            if length < 0 or pos + length > len(data):
                raise SlpError("Truncated status response")
            text = data[pos : pos + length].decode("utf-8", errors="replace")
            return reader, writer, parse_status(text), elapsed
        except BaseException:
            writer.close()
            raise

    reader, writer, result, elapsed = await asyncio.wait_for(status(), timeout)
    latency = elapsed
    try:
        payload = struct.pack(">q", int(time.time() * 1000))
        writer.write(packet(0x01, payload))
        await writer.drain()
        sent = time.monotonic()
        remaining = min(PONG_TIMEOUT, max(0.0, deadline - sent))
        packet_id, data, pos = await asyncio.wait_for(_read_packet(reader), remaining)
        if packet_id == 0x01 and data[pos:] == payload:
            latency = time.monotonic() - sent
    except (TimeoutError, OSError, SlpError, asyncio.IncompleteReadError):
        pass  # some servers close after the status: the status round trip is the latency then
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), max(0.05, min(0.5, deadline - time.monotonic())))
    result["latency_ms"] = round(latency * 1000, 2)
    return result
