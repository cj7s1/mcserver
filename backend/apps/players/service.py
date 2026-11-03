"""Minecraft player management (whitelist, operators, bans, kicks).

While the server runs, changes go through RCON so the server applies them
immediately (and persists them itself). While it is stopped, the JSON files in the
instance directory are edited directly, using Mojang (online mode) or offline
UUIDs (offline mode) for new entries.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from django.conf import settings

from apps.core.errors import ApiError, UpstreamError
from apps.instances import properties
from apps.instances.models import Instance
from apps.nodes.client import AgentClient

NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
LIST_RE = re.compile(r"There are (\d+)(?: of a max of| out of maximum| of a max) (\d+) players online[:.]?\s*(.*)", re.I | re.S)
FILES = {"whitelist": "whitelist.json", "ops": "ops.json", "bans": "banned-players.json", "ip_bans": "banned-ips.json"}
COLOR_CODES = re.compile(r"§.")


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise ApiError("Player names are 1-16 characters: letters, digits and underscores", code="invalid_player_name")
    return name


def _running(instance: Instance) -> bool:
    status = AgentClient(instance.node).status(instance.id)
    return bool(status.get("running"))


def rcon(instance: Instance, command: str) -> str:
    return COLOR_CODES.sub("", AgentClient(instance.node).command(instance.id, command))


def online_players(instance: Instance) -> dict[str, Any]:
    out = rcon(instance, "list")
    m = LIST_RE.search(out)
    if not m:
        return {"online": None, "max": None, "players": [], "raw": out[:500]}
    names = [n.strip() for n in m.group(3).replace("\n", ",").split(",") if n.strip()]
    return {"online": int(m.group(1)), "max": int(m.group(2)), "players": names}


def _read_json(instance: Instance, path: str) -> list[dict[str, Any]]:
    try:
        content = AgentClient(instance.node).read_text(instance.id, path)["content"]
    except ApiError as exc:
        if exc.error_code == "not_found":
            return []
        raise
    try:
        data = json.loads(content or "[]")
    except json.JSONDecodeError as exc:
        raise ApiError(f"{path} is not valid JSON; fix it in the file manager", code="invalid_json") from exc
    return data if isinstance(data, list) else []


def _write_json(instance: Instance, path: str, data: list[dict[str, Any]]) -> None:
    AgentClient(instance.node).write_text(instance.id, path, json.dumps(data, indent=2) + "\n")


def offline_uuid(name: str) -> str:
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode()).digest())  # noqa: S324 - Minecraft's algorithm
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def lookup_profile(instance: Instance, name: str) -> dict[str, str]:
    online_mode = True
    try:
        raw = AgentClient(instance.node).read_text(instance.id, "server.properties")["content"]
        online_mode = properties.parse(raw).get("online-mode", "true").lower() != "false"
    except ApiError:
        pass
    if not online_mode:
        return {"uuid": offline_uuid(name), "name": name}
    try:
        resp = httpx.get(
            f"https://api.mojang.com/users/profiles/minecraft/{name}",
            headers={"User-Agent": settings.MCPANEL_USER_AGENT},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        raise UpstreamError(f"Could not look up the player with Mojang: {exc}", code="mojang_unreachable") from exc
    if resp.status_code in (204, 404):
        raise ApiError(f"No Minecraft account named '{name}' exists", code="player_not_found", status_code=404)
    if resp.status_code >= 400:
        raise UpstreamError(f"Mojang profile lookup failed (HTTP {resp.status_code})", code="mojang_error")
    data = resp.json()
    raw_id = data["id"]
    return {"uuid": str(uuid.UUID(raw_id)), "name": data.get("name", name)}


def lists(instance: Instance) -> dict[str, Any]:
    client_running = _running(instance)
    result: dict[str, Any] = {"running": client_running}
    for key, path in FILES.items():
        result[key] = _read_json(instance, path)
    result["known_players"] = _read_json(instance, "usercache.json")[:500]
    if client_running:
        try:
            result["online"] = online_players(instance)
        except ApiError as exc:
            result["online"] = {"error": str(exc.detail)}
    return result


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S +0000")


def mutate(instance: Instance, action: str, name: str, reason: str = "") -> dict[str, Any]:
    """action: whitelist_add, whitelist_remove, op, deop, ban, pardon, kick"""
    name = validate_name(name)
    reason = re.sub(r"[\r\n]", " ", reason or "")[:200]
    running = _running(instance)
    if running:
        commands = {
            "whitelist_add": f"whitelist add {name}",
            "whitelist_remove": f"whitelist remove {name}",
            "op": f"op {name}",
            "deop": f"deop {name}",
            "ban": f"ban {name} {reason}".strip(),
            "pardon": f"pardon {name}",
            "kick": f"kick {name} {reason}".strip(),
        }
        if action not in commands:
            raise ApiError("Unknown player action", code="invalid_action")
        output = rcon(instance, commands[action])
        return {"via": "rcon", "output": output}
    if action == "kick":
        raise ApiError("The server is not running, so nobody can be kicked", code="not_running", status_code=409)
    key = {
        "whitelist_add": "whitelist",
        "whitelist_remove": "whitelist",
        "op": "ops",
        "deop": "ops",
        "ban": "bans",
        "pardon": "bans",
    }.get(action)
    if key is None:
        raise ApiError("Unknown player action", code="invalid_action")
    path = FILES[key]
    entries = _read_json(instance, path)
    remove = action in ("whitelist_remove", "deop", "pardon")
    lowered = name.lower()
    if remove:
        new = [e for e in entries if str(e.get("name", "")).lower() != lowered]
        if len(new) == len(entries):
            raise ApiError(f"{name} is not in {path}", code="not_listed", status_code=404)
        _write_json(instance, path, new)
        return {"via": "file", "output": f"Removed {name} from {path}"}
    if any(str(e.get("name", "")).lower() == lowered for e in entries):
        return {"via": "file", "output": f"{name} is already in {path}"}
    profile = lookup_profile(instance, name)
    entry: dict[str, Any] = {"uuid": profile["uuid"], "name": profile["name"]}
    if key == "ops":
        entry.update({"level": 4, "bypassesPlayerLimit": False})
    elif key == "bans":
        entry.update({"created": _now(), "source": "MCPanel", "expires": "forever", "reason": reason or "Banned by an operator."})
    entries.append(entry)
    _write_json(instance, path, entries)
    return {"via": "file", "output": f"Added {profile['name']} to {path} (applies when the server starts)"}
