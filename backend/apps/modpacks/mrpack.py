"""Pre-deployment inspection of a Modrinth .mrpack using HTTP range requests.

Reads only the zip central directory plus a few small entries
(``modrinth.index.json`` and known "manual download" manifests) instead of the
whole pack, so the wizard can warn about problems before anything is deployed.
Verified: cdn.modrinth.com honours explicit ``bytes=start-end`` ranges (suffix
ranges are not supported).
"""

from __future__ import annotations

import json
import logging
import struct
import zlib
from typing import Any
from urllib.parse import urlparse

import httpx
from django.conf import settings
from django.core.cache import cache

log = logging.getLogger(__name__)

ALLOWED_HOSTS = {"cdn.modrinth.com"}
TAIL_BYTES = 128 * 1024
MAX_CD_BYTES = 8 * 1024 * 1024
MAX_ENTRY_BYTES = 4 * 1024 * 1024
EOCD_SIG = b"PK\x05\x06"
CDH_SIG = b"PK\x01\x02"
LFH_SIG = b"PK\x03\x04"
CHECKER_PATHS = ("overrides/config/missing_mods_checker.json", "server-overrides/config/missing_mods_checker.json")


class InspectError(Exception):
    pass


def _range(client: httpx.Client, url: str, start: int, end: int) -> bytes:
    resp = client.get(url, headers={"Range": f"bytes={start}-{end}"})
    if resp.status_code != 206:
        raise InspectError(f"CDN did not honour the range request (HTTP {resp.status_code})")
    return resp.content


def _central_directory(client: httpx.Client, url: str, size: int) -> dict[str, tuple[int, int, int, int]]:
    tail_start = max(0, size - TAIL_BYTES)
    tail = _range(client, url, tail_start, size - 1)
    pos = tail.rfind(EOCD_SIG)
    if pos < 0:
        raise InspectError("Not a zip file (no end-of-central-directory record)")
    _, _, _, _, total, cd_size, cd_offset, _ = struct.unpack("<4sHHHHIIH", tail[pos : pos + 22])
    if cd_offset == 0xFFFFFFFF or cd_size > MAX_CD_BYTES:
        raise InspectError("Zip64 or oversized central directory is not supported for preview")
    if cd_offset >= tail_start:
        cd = tail[cd_offset - tail_start : cd_offset - tail_start + cd_size]
    else:
        cd = _range(client, url, cd_offset, cd_offset + cd_size - 1)
    entries: dict[str, tuple[int, int, int, int]] = {}
    p = 0
    for _ in range(total):
        if cd[p : p + 4] != CDH_SIG:
            break
        (method, _t, _d, _crc, csize, usize, nlen, elen, clen, _disk, _ia, _ea, offset) = struct.unpack(
            "<HHHIIIHHHHHII", cd[p + 10 : p + 46]
        )
        name = cd[p + 46 : p + 46 + nlen].decode("utf-8", errors="replace")
        entries[name] = (method, csize, usize, offset)
        p += 46 + nlen + elen + clen
    return entries


def _read_entry(client: httpx.Client, url: str, entry: tuple[int, int, int, int]) -> bytes:
    method, csize, usize, offset = entry
    if csize > MAX_ENTRY_BYTES or usize > MAX_ENTRY_BYTES:
        raise InspectError("Entry too large to preview")
    header = _range(client, url, offset, offset + 29)
    if header[:4] != LFH_SIG:
        raise InspectError("Corrupt local file header")
    nlen, elen = struct.unpack("<HH", header[26:30])
    start = offset + 30 + nlen + elen
    data = _range(client, url, start, start + csize - 1) if csize else b""
    if method == 0:
        return data
    if method == 8:
        out = zlib.decompressobj(-15).decompress(data, MAX_ENTRY_BYTES + 1)
        if len(out) > MAX_ENTRY_BYTES:
            raise InspectError("Entry too large to preview")
        return out
    raise InspectError(f"Unsupported compression method {method}")


def inspect_mrpack(url: str, sha1: str | None = None) -> dict[str, Any]:
    """Return a summary of the pack, or {'available': False, 'reason': ...} if it cannot be inspected."""
    host = (urlparse(url).hostname or "").lower()
    if urlparse(url).scheme != "https" or host not in ALLOWED_HOSTS:
        return {"available": False, "reason": "Only Modrinth CDN packs can be previewed"}
    key = f"mrpack:inspect:{sha1 or url}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        with httpx.Client(headers={"User-Agent": settings.MCPANEL_USER_AGENT}, timeout=20, follow_redirects=False) as client:
            probe = client.get(url, headers={"Range": "bytes=0-0"})
            content_range = probe.headers.get("content-range", "")
            if probe.status_code != 206 or "/" not in content_range:
                raise InspectError("CDN did not report the file size")
            size = int(content_range.rsplit("/", 1)[1])
            entries = _central_directory(client, url, size)
            index_entry = entries.get("modrinth.index.json")
            if index_entry is None:
                raise InspectError("modrinth.index.json missing from the pack")
            index = json.loads(_read_entry(client, url, index_entry))
            if not isinstance(index, dict):  # (a list or a scalar was a 500 below)
                raise InspectError("modrinth.index.json is not an object")
            checker = None
            for path in CHECKER_PATHS:
                if path in entries:
                    try:
                        checker = json.loads(_read_entry(client, url, entries[path]))
                    except (InspectError, json.JSONDecodeError):
                        checker = []
                    break
    except (httpx.HTTPError, InspectError, ValueError, struct.error) as exc:
        result = {"available": False, "reason": f"Could not preview the pack: {exc}"}
        cache.set(key, result, 600)
        return result

    # whatever the pack's index holds: only objects count (a malformed pack answers "not available", never a 500)
    files = [f for f in (index.get("files") or []) if isinstance(f, dict)] if isinstance(index.get("files"), list) else []
    server_unsupported = [f for f in files if isinstance(f.get("env"), dict) and f["env"].get("server") == "unsupported"]
    override_mods = [n for n in entries if n.startswith(("overrides/mods/", "server-overrides/mods/")) and n.endswith(".jar")]
    deps = index.get("dependencies") if isinstance(index.get("dependencies"), dict) else {}
    warnings: list[str] = []
    manual = [
        {"name": e.get("displayName") or e.get("pattern"), "filename": e.get("pattern"), "url": e.get("url")}
        for e in (checker if isinstance(checker, list) else [])
        if isinstance(e, dict)
    ]
    if checker is not None:
        warnings.append(
            f"This release ships a 'Missing Mods Checker' that expects {len(manual)} file(s) to be downloaded "
            "manually (usually from CurseForge). The server will not start until they are added. "
            "If the pack is also published on CurseForge, that edition installs them automatically."
        )
    loader = next((k for k in ("neoforge", "forge", "fabric-loader", "quilt-loader") if k in deps), None)
    result = {
        "available": True,
        "pack_name": index.get("name"),
        "minecraft_version": deps.get("minecraft"),
        "loader": {"neoforge": "neoforge", "forge": "forge", "fabric-loader": "fabric", "quilt-loader": "quilt"}.get(
            loader or ""
        ),
        "loader_version": deps.get(loader) if loader else None,
        "files_total": len(files),
        "files_server_unsupported": len(server_unsupported),
        "override_mods": len(override_mods),
        "pack_size_bytes": size,
        "manual_downloads": manual,
        "warnings": warnings,
    }
    cache.set(key, result, 24 * 3600)
    return result
