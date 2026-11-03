"""Minimal asynchronous Source RCON client (as implemented by Minecraft Java Edition)."""

from __future__ import annotations

import asyncio
import itertools
import struct

TYPE_AUTH = 3
TYPE_COMMAND = 2
TYPE_RESPONSE = 0
MAX_COMMAND_LEN = 1446  # Minecraft rejects longer RCON payloads
_ids = itertools.count(1)


class RconError(Exception):
    pass


class RconClient:
    def __init__(self, host: str, port: int, password: str, timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def __aenter__(self) -> RconClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=self.timeout
            )
        except (OSError, TimeoutError) as exc:
            raise RconError(f"Cannot connect to RCON at {self.host}:{self.port}: {exc}") from exc
        req_id = await self._send(TYPE_AUTH, self.password)
        resp_id, _type, _payload = await self._read_packet(self.timeout)
        if resp_id == -1 or resp_id != req_id:
            raise RconError("RCON authentication failed")

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except OSError:
                pass
        self._reader = self._writer = None

    async def _send(self, ptype: int, payload: str) -> int:
        assert self._writer is not None
        req_id = next(_ids) & 0x7FFFFFFF
        body = struct.pack("<ii", req_id, ptype) + payload.encode("utf-8") + b"\x00\x00"
        self._writer.write(struct.pack("<i", len(body)) + body)
        await self._writer.drain()
        return req_id

    async def _read_packet(self, timeout: float) -> tuple[int, int, str]:
        assert self._reader is not None
        try:
            raw_len = await asyncio.wait_for(self._reader.readexactly(4), timeout=timeout)
            (length,) = struct.unpack("<i", raw_len)
            if length < 10 or length > 1024 * 1024:
                raise RconError("Malformed RCON packet")
            data = await asyncio.wait_for(self._reader.readexactly(length), timeout=timeout)
        except asyncio.IncompleteReadError as exc:
            raise RconError("RCON connection closed") from exc
        except TimeoutError as exc:
            raise RconError("Timed out waiting for RCON response") from exc
        req_id, ptype = struct.unpack("<ii", data[:8])
        return req_id, ptype, data[8:-2].decode("utf-8", errors="replace")

    async def command(self, command: str, timeout: float | None = None) -> str:
        if len(command.encode("utf-8")) > MAX_COMMAND_LEN:
            raise RconError("Command is too long for RCON")
        if "\n" in command or "\r" in command:
            raise RconError("Commands must be a single line")
        req_id = await self._send(TYPE_COMMAND, command)
        rid, _t, payload = await self._read_packet(timeout or max(self.timeout, 30.0))
        if rid != req_id:
            raise RconError("Unexpected RCON response id")
        parts = [payload]
        # Minecraft fragments responses at 4096 bytes; keep reading while fragments are full.
        while len(parts[-1].encode("utf-8")) >= 4000:
            try:
                _rid, _t, more = await self._read_packet(0.5)
            except RconError:
                break
            parts.append(more)
        return "".join(parts)


async def run_command(host: str, port: int, password: str, command: str, timeout: float = 30.0) -> str:
    async with RconClient(host, port, password) as client:
        return await client.command(command, timeout=timeout)
