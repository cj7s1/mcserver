"""What the instance-network source guard knows while Docker cannot be asked.

The agent refuses API requests from the Minecraft containers' networks (``main._RefuseInstanceNetworks``).
Their subnets come from Docker. When Docker does not answer at start-up, the guard falls back to
what it saw the last time it could ask (stored in ``<state dir>/instance-networks.json``), checked
against the networks this process is attached to right now (read from the kernel's routing
table, no Docker needed):

* an address in a subnet that was an instance network last time is refused (403);
* an address in a subnet this process is attached to is allowed only if that subnet was a
  non-instance network last time (the panel's internal network); an attached subnet it has not
  seen before may be a recreated instance network, so it is refused until Docker answers (503);
* an address that is in none of the attached subnets (loopback aside: a remote panel reaching a
  published node agent) passes if it was not blocked last time;
* without a stored record only loopback and the health probe pass.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import os
import socket
import struct
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
STATE_FILE = "instance-networks.json"
_ROUTE = Path("/proc/net/route")
_ROUTE6 = Path("/proc/net/ipv6_route")


def attached_subnets(route: Path = _ROUTE, route6: Path = _ROUTE6) -> list[Network]:
    """The subnets this process reaches directly (routes without a gateway), loopback and
    link-local left out. Empty when the routing table cannot be read."""
    out: set[Network] = set()
    with contextlib.suppress(OSError):
        for line in route.read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) < 8 or fields[2] != "00000000" or fields[7] == "00000000":
                continue
            with contextlib.suppress(ValueError, struct.error):
                dest = socket.inet_ntoa(struct.pack("<I", int(fields[1], 16)))
                mask = socket.inet_ntoa(struct.pack("<I", int(fields[7], 16)))
                out.add(ipaddress.ip_network(f"{dest}/{mask}", strict=False))
    with contextlib.suppress(OSError):
        for line in route6.read_text().splitlines():
            fields = line.split()
            if len(fields) < 10 or fields[9] == "lo" or set(fields[4]) != {"0"}:
                continue
            with contextlib.suppress(ValueError):
                prefix = int(fields[1], 16)
                if not 0 < prefix < 128:
                    continue
                dest = ipaddress.IPv6Address(bytes.fromhex(fields[0]))
                out.add(ipaddress.ip_network(f"{dest}/{prefix}", strict=False))
    return sorted(
        (n for n in out if not (n.is_loopback or n.is_link_local or n.is_multicast)),
        key=lambda n: (n.version, n),
    )


def _overlaps(net: Network, others: list[Network]) -> bool:
    return any(net.version == o.version and net.overlaps(o) for o in others)  # type: ignore[arg-type]


def remember(state_dir: Path, blocked: list[Network], attached: list[Network]) -> None:
    """Store the instance subnets just read from Docker and the attached subnets that are not one."""
    record = {
        "blocked": sorted({str(n) for n in blocked}),
        "trusted": sorted({str(n) for n in attached if not _overlaps(n, blocked)}),
    }
    path = state_dir / STATE_FILE
    try:
        if path.exists() and json.loads(path.read_text()) == record:
            return
    except (OSError, ValueError):
        pass
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(record, fh)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("Could not store the instance network subnets in %s: %s", path, exc)


def recall(state_dir: Path) -> dict[str, list[Network]] | None:
    """The record :func:`remember` stored; None when there is none (or it is unreadable)."""
    try:
        data: Any = json.loads((state_dir / STATE_FILE).read_text())
        return {
            key: [ipaddress.ip_network(v, strict=False) for v in data[key] if isinstance(v, str)]
            for key in ("blocked", "trusted")
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None


def provisional_verdict(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    record: dict[str, list[Network]] | None,
    attached: list[Network],
) -> str | None:
    """The guard's answer while Docker cannot be asked: None (pass), "blocked" or "pending"."""
    if record is None:
        return "pending"
    if any(ip in n for n in record["blocked"]):
        return "blocked"
    for net in attached:
        if ip in net:
            return None if net in record["trusted"] else "pending"
    return None
