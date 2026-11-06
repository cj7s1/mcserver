"""World folder helpers for world import/export (``GET /world``, ``POST /world/replace``).

The world folder is ``level-name`` from server.properties (default ``world``), unless the
user set itzg's ``LEVEL`` variable, which itzg writes into server.properties on every
start and therefore wins. Bukkit-based servers (Paper, Purpur, Folia, Spigot) keep the
nether and the end in sibling folders (``world_nether``, ``world_the_end``); every other
server keeps them inside the world folder (``world/DIM-1``, ``world/DIM1``).

The agent re-validates the level name and does the actual work (``agent/mcagent/world.py``);
the checks here only turn mistakes into clear errors before anything is uploaded.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from apps.core.errors import ApiError
from apps.nodes.client import AgentClient

from . import properties
from .models import PLUGIN_TYPES, Instance

DEFAULT_LEVEL = "world"
# keep in sync with agent/mcagent/world.py RESERVED_FOLDERS
RESERVED_FOLDERS = frozenset(
    {
        "mods",
        "plugins",
        "config",
        "defaultconfigs",
        "libraries",
        "logs",
        "crash-reports",
        "versions",
        "cache",
        "kubejs",
        "scripts",
        "resourcepacks",
        "downloads",
        "backups",
        "server.properties",
    }
)


def validate_level_name(name: str) -> str:
    """Normalise ``level-name``; raise a clear ApiError for names the agent would refuse."""
    raw = name if isinstance(name, str) else ""
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    bad = (
        not raw.strip()
        or "\\" in raw
        or "\x00" in raw
        or len(raw) > 200
        or not parts
        or len(parts) > 4
        or any(p == ".." or p.startswith(".") or any(ord(c) < 32 for c in p) or len(p) > 255 for p in parts)
    )
    if bad:
        raise ApiError(
            f"The world folder name {raw!r} (level-name) is not supported; use a plain folder name such as 'world'",
            code="invalid_level_name",
        )
    if parts[0].lower() in RESERVED_FOLDERS:
        raise ApiError(
            f"level-name is '{raw}', which is a server folder, not a world folder. Fix level-name in server.properties first.",
            code="invalid_level_name",
        )
    return "/".join(parts)


def level_name(instance: Instance, client: AgentClient) -> tuple[str, str]:
    """(world folder, where it came from: ``env`` | ``server.properties`` | ``default``)."""
    env_level = str((instance.env or {}).get("LEVEL") or "").strip()
    if env_level:
        return env_level, "env"
    try:
        raw = client.read_text(instance.id, "server.properties")["content"]
    except ApiError as exc:
        if exc.error_code not in ("not_found", "not_a_directory"):
            raise
        raw = ""
    value = properties.parse(raw).get("level-name", "").strip()
    return (value, "server.properties") if value else (DEFAULT_LEVEL, "default")


def layout(instance: Instance, client: AgentClient) -> str:
    """``bukkit`` for Paper-family servers (or any server folder with a bukkit.yml), else ``vanilla``."""
    if instance.server_type in PLUGIN_TYPES:
        return "bukkit"
    try:
        entry = client.stat_file(instance.id, "bukkit.yml")
    except ApiError:
        return "vanilla"
    return "bukkit" if entry.get("type") == "file" else "vanilla"


def warnings_for(instance: Instance) -> list[str]:
    env = instance.env or {}
    out = []
    if env.get("WORLD"):
        out.append(
            "The WORLD environment variable is set: the runtime copies that world in on start when the world folder "
            "is missing (or always, with FORCE_WORLD_COPY), which can undo a replaced world."
        )
    return out


def download_url(instance: Instance, paths: list[str], level: str) -> str:
    """Link to the zip export (reuses ``GET /files/archive``; several folders for bukkit layouts)."""
    name = f"{instance.name}-{level.rsplit('/', 1)[-1]}"
    query = urlencode([*(("path", p) for p in paths), ("name", name), ("flush", "true")])
    return f"/api/instances/{instance.id}/files/archive?{query}"


def describe(instance: Instance, client: AgentClient) -> dict[str, Any]:
    level, source = level_name(instance, client)
    world_layout = layout(instance, client)
    data: dict[str, Any] = {
        "level_name": level,
        "level_name_source": source,
        "layout": world_layout,
        "folders": [],
        "total_bytes": 0,
        "exists": False,
        "leftovers": [],
        "download_url": None,
        "warnings": warnings_for(instance),
        "error": None,
    }
    try:
        validate_level_name(level)
    except ApiError as exc:
        data["error"] = {"code": exc.error_code, "message": str(exc.detail)}
        return data
    try:
        info = client.world_info(instance.id, level, world_layout)
    except ApiError as exc:
        if exc.error_code != "not_found":
            raise
        info = {"folders": [], "total_bytes": 0, "leftovers": []}
    data.update(folders=info["folders"], total_bytes=info["total_bytes"], leftovers=info.get("leftovers", []))
    present = [f["path"] for f in info["folders"] if f.get("exists")]
    data["exists"] = any(f.get("role") == "overworld" and f.get("has_level_dat") for f in info["folders"])
    if present:
        data["download_url"] = download_url(instance, present, level)
    return data
