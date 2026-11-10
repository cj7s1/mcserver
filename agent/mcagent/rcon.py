"""Minimal asynchronous Source RCON client (as implemented by Minecraft Java Edition).

Generic over the game: the port, the password and the longest command a server accepts
(``max_command_len``, Minecraft's limit by default) are the caller's; the control drivers in
``mcagent.drivers`` supply them per game.

The peer is the Minecraft process, which runs third-party code (mods, plugins), so it is
treated as untrusted: packets, the total size of a (fragmented) response and the time a
command may take are all bounded, and socket errors surface as :class:`RconError`.
"""

from __future__ import annotations

import asyncio
import itertools
import struct
import time

TYPE_AUTH = 3
TYPE_COMMAND = 2
TYPE_RESPONSE = 0
MAX_COMMAND_LEN = 1446  # Minecraft rejects longer RCON payloads
# Vanilla sends at most 4096 payload bytes per packet; anything far beyond that is not RCON.
MAX_PACKET_BYTES = 1024 * 1024
# A whole (fragmented) command response; larger output is truncated and the connection dropped.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
FRAGMENT_THRESHOLD = 4000  # a fragment this large may be followed by another one
CONTINUATION_TIMEOUT = 0.5
TRUNCATED_NOTE = "\n[mcpanel] output truncated"
_ids = itertools.count(1)


class RconError(Exception):
    pass


class RconUnavailable(RconError):
    """RCON could not be reached or did not accept the login: no command was sent."""


class RconClient:
    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        timeout: float = 5.0,
        *,
        max_command_len: int = MAX_COMMAND_LEN,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.max_command_len = max_command_len
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
            raise RconUnavailable(f"Cannot connect to RCON at {self.host}:{self.port}: {exc}") from exc
        try:
            req_id = await self._send(TYPE_AUTH, self.password)
            resp_id, _type, _payload = await self._read_packet(self.timeout)
        except RconError as exc:
            raise RconUnavailable(str(exc)) from exc
        if resp_id == -1 or resp_id != req_id:
            raise RconUnavailable("RCON authentication failed")

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except OSError:
                pass
        self._reader = self._writer = None

    def abort(self) -> None:
        """Drop the connection without waiting for the peer; never blocks."""
        if self._writer is not None:
            self._writer.transport.abort()
        self._reader = self._writer = None

    async def _send(self, ptype: int, payload: str) -> int:
        assert self._writer is not None
        req_id = next(_ids) & 0x7FFFFFFF
        body = struct.pack("<ii", req_id, ptype) + payload.encode("utf-8") + b"\x00\x00"
        try:
            self._writer.write(struct.pack("<i", len(body)) + body)
            await asyncio.wait_for(self._writer.drain(), timeout=self.timeout)
        except (OSError, TimeoutError) as exc:
            raise RconError(f"Cannot send RCON packet: {exc}") from exc
        return req_id

    async def _read_packet(self, timeout: float) -> tuple[int, int, str]:
        """One packet; ``timeout`` bounds the whole packet, not each read."""
        assert self._reader is not None
        if timeout <= 0:
            raise RconError("Timed out waiting for RCON response")
        try:
            async with asyncio.timeout(timeout):
                raw_len = await self._reader.readexactly(4)
                (length,) = struct.unpack("<i", raw_len)
                if length < 10 or length > MAX_PACKET_BYTES:
                    raise RconError("Malformed RCON packet")
                data = await self._reader.readexactly(length)
        except asyncio.IncompleteReadError as exc:
            raise RconError("RCON connection closed") from exc
        except TimeoutError as exc:
            raise RconError("Timed out waiting for RCON response") from exc
        except OSError as exc:
            raise RconError(f"RCON connection failed: {exc}") from exc
        req_id, ptype = struct.unpack("<ii", data[:8])
        return req_id, ptype, data[8:-2].decode("utf-8", errors="replace")

    async def command(self, command: str, timeout: float | None = None) -> str:
        """Run a command; the whole response (all fragments) must arrive within ``timeout``."""
        if len(command.encode("utf-8")) > self.max_command_len:
            raise RconError("Command is too long for RCON")
        if "\n" in command or "\r" in command:
            raise RconError("Commands must be a single line")
        budget = timeout or max(self.timeout, 30.0)
        deadline = time.monotonic() + budget
        req_id = await self._send(TYPE_COMMAND, command)
        rid, _t, payload = await self._read_packet(deadline - time.monotonic())
        if rid != req_id:
            raise RconError("Unexpected RCON response id")
        parts = [payload]
        received = len(payload.encode("utf-8"))
        # Minecraft fragments responses at 4096 bytes; keep reading while fragments are full,
        # but never past the size budget or the command's deadline (a mod controls the peer).
        last = received
        while last >= FRAGMENT_THRESHOLD:
            remaining = deadline - time.monotonic()
            if received >= MAX_RESPONSE_BYTES or remaining <= 0:
                parts.append(TRUNCATED_NOTE)
                break
            try:
                _rid, _t, more = await self._read_packet(min(CONTINUATION_TIMEOUT, remaining))
            except RconError:
                if time.monotonic() >= deadline:  # cut short by the deadline, not the end of output
                    parts.append(TRUNCATED_NOTE)
                break
            last = len(more.encode("utf-8"))
            if received + last > MAX_RESPONSE_BYTES:
                parts.append(TRUNCATED_NOTE)
                break
            received += last
            parts.append(more)
        return "".join(parts)


async def run_command(
    host: str,
    port: int,
    password: str,
    command: str,
    timeout: float = 30.0,
    *,
    max_command_len: int = MAX_COMMAND_LEN,
) -> str:
    async with RconClient(host, port, password, max_command_len=max_command_len) as client:
        return await client.command(command, timeout=timeout)


def run_command_sync(
    host: str,
    port: int,
    password: str,
    command: str,
    *,
    timeout: float = 30.0,
    connect_timeout: float = 5.0,
    max_command_len: int = MAX_COMMAND_LEN,
) -> str:
    """:func:`run_command` for worker threads: runs its own event loop, so never call it from one.

    Hard-bounded, whatever the peer does. Connecting and logging in may take ``connect_timeout``
    in total; if RCON has not accepted the login by then :class:`RconUnavailable` is raised and
    nothing was sent. The command may then take ``timeout``. The connection is dropped rather
    than closed gracefully, so a peer that stops reading cannot hold the caller either.
    ``host`` must be an IP address (as ``docker_ops.rcon_endpoint`` returns): a name would be
    resolved in a helper thread that the event loop waits for when it shuts down.
    """

    async def _run() -> str:
        client = RconClient(host, port, password, timeout=connect_timeout, max_command_len=max_command_len)
        try:
            try:
                async with asyncio.timeout(connect_timeout):
                    await client.connect()
            except TimeoutError as exc:
                raise RconUnavailable(
                    f"RCON at {host}:{port} did not answer within {connect_timeout:g} s"
                ) from exc
            try:
                # command() keeps to ``timeout`` itself; this is the backstop for its send
                async with asyncio.timeout(timeout + 1.0):
                    return await client.command(command, timeout=timeout)
            except TimeoutError as exc:
                raise RconError("Timed out waiting for RCON response") from exc
        finally:
            client.abort()

    return asyncio.run(_run())
