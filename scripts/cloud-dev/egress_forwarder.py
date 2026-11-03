#!/usr/bin/env python3
"""TCP forwarder used ONLY in sandboxed CI/cloud development environments.

Some sandboxes only allow outbound HTTPS through a proxy listening on the host's
loopback interface, which bridge-networked containers cannot reach. This exposes
that proxy on another address so Minecraft containers can download server jars.
Never needed on a normal Docker host (e.g. Unraid).

usage: egress_forwarder.py LISTEN_HOST LISTEN_PORT TARGET_HOST TARGET_PORT
"""

import asyncio
import sys


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def main() -> None:
    listen_host, listen_port, target_host, target_port = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            t_reader, t_writer = await asyncio.open_connection(target_host, target_port)
        except OSError:
            writer.close()
            return
        await asyncio.gather(pipe(reader, t_writer), pipe(t_reader, writer))

    server = await asyncio.start_server(handle, listen_host, listen_port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
