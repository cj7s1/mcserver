"""Hangar (hangar.papermc.io) plugin adapter: PaperMC's official plugin repository.

Verified endpoints (2026-11): ``GET /api/v1/projects?q=&platform=&version=&limit=&offset=``
returns ``{pagination, result[]}`` with ``namespace{owner,slug}``, ``stats``,
``supportedPlatforms{PAPER:[mc...]}``; ``GET /api/v1/projects/{slug}/versions?platform=&platformVersion=``
returns versions with ``channel.name``, ``platformDependencies{PAPER:[mc...]}``,
``downloads{PAPER:{fileInfo{name,sizeBytes,sha256Hash}, downloadUrl, externalUrl}}`` and
``pluginDependencies{PAPER:[{name, required, externalUrl}]}``.

Hangar publishes SHA-256 hashes only; files are served from hangarcdn.papermc.io. Versions that
only link to an external site (``externalUrl``) cannot be installed automatically.
"""

from __future__ import annotations

import re
from typing import Any

from apps.core.errors import ApiError

from .providers import _cached, _request

HANGAR_API = "https://hangar.papermc.io/api/v1"
SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
PLATFORMS = {"PAPER": "PAPER", "PURPUR": "PAPER", "FOLIA": "PAPER"}  # Hangar publishes Paper-family builds as PAPER


def _slug(value: str) -> str:
    if not SLUG_RE.match(value or ""):
        raise ApiError("Invalid Hangar project", code="invalid_project")
    return value


def platform_for(server_type: str) -> str:
    try:
        return PLATFORMS[server_type]
    except KeyError as exc:
        raise ApiError("Hangar only provides Paper-family plugins", code="unsupported_platform") from exc


def _project(p: dict[str, Any]) -> dict[str, Any]:
    ns = p.get("namespace") or {}
    stats = p.get("stats") or {}
    return {
        "provider": "hangar",
        "id": ns.get("slug") or p.get("name"),
        "slug": ns.get("slug"),
        "title": p.get("name"),
        "description": p.get("description"),
        "author": ns.get("owner"),
        "icon_url": p.get("avatarUrl"),
        "downloads": stats.get("downloads"),
        "follows": stats.get("stars"),
        "categories": [p.get("category")] if p.get("category") else [],
        "loaders": [k.lower() for k in (p.get("supportedPlatforms") or {})],
        "game_versions": list(reversed((p.get("supportedPlatforms") or {}).get("PAPER", []))),
        "updated": p.get("lastUpdated"),
        "url": f"https://hangar.papermc.io/{ns.get('owner')}/{ns.get('slug')}",
        "server_side": "required",
    }


def search(query: str, *, server_type: str, minecraft_version: str, offset: int = 0, limit: int = 20) -> dict[str, Any]:
    params: dict[str, Any] = {"q": query[:100], "platform": platform_for(server_type), "limit": min(limit, 25),
                              "offset": max(0, offset), "sort": "-downloads"}
    if minecraft_version and minecraft_version.upper() != "LATEST":
        params["version"] = minecraft_version
    key = f"hangar:search:{hash(tuple(sorted(params.items())))}"
    data = _cached(key, 120, lambda: _request("GET", f"{HANGAR_API}/projects", params=params, provider="Hangar"))
    return {"total": (data.get("pagination") or {}).get("count", 0), "offset": offset, "limit": limit,
            "results": [_project(p) for p in data.get("result", [])]}


def _version(v: dict[str, Any], platform: str) -> dict[str, Any]:
    dl = (v.get("downloads") or {}).get(platform) or {}
    info = dl.get("fileInfo") or {}
    deps = (v.get("pluginDependencies") or {}).get(platform) or []
    channel = ((v.get("channel") or {}).get("name") or "").lower()
    return {
        "provider": "hangar",
        "id": v.get("name"),
        "name": v.get("name"),
        "version_number": v.get("name"),
        "version_type": "release" if channel == "release" else ("beta" if channel in ("beta", "snapshot") else channel or "release"),
        "game_versions": (v.get("platformDependencies") or {}).get(platform, []),
        "loaders": ["paper"],
        "published": v.get("createdAt"),
        "file": {"filename": info.get("name"), "url": dl.get("downloadUrl"), "size": info.get("sizeBytes"),
                 "sha256": info.get("sha256Hash"), "external_url": dl.get("externalUrl")} if dl else None,
        "dependencies": [{"name": d.get("name"), "type": "required" if d.get("required") else "optional",
                          "external_url": d.get("externalUrl")} for d in deps],
    }


def versions(slug: str, *, server_type: str, minecraft_version: str) -> list[dict[str, Any]]:
    platform = platform_for(server_type)
    params: dict[str, Any] = {"platform": platform, "limit": 25, "offset": 0}
    if minecraft_version and minecraft_version.upper() != "LATEST":
        params["platformVersion"] = minecraft_version
    key = f"hangar:versions:{_slug(slug)}:{hash(tuple(sorted(params.items())))}"
    data = _cached(key, 300, lambda: _request("GET", f"{HANGAR_API}/projects/{slug}/versions", params=params,
                                               provider="Hangar"))
    out = [_version(v, platform) for v in data.get("result", []) if v.get("visibility", "public") == "public"]
    if minecraft_version and minecraft_version.upper() != "LATEST":
        out = [v for v in out if minecraft_version in v["game_versions"]]  # re-check locally
    return out
