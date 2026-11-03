"""Modpack/mod provider adapters (see docs/reference/*-api.md for verified API details).

Each adapter normalises upstream data into plain dicts:

* project: id, slug, title, description, author, icon_url, downloads, follows, categories,
  loaders, game_versions, updated, url, provider, server_side, license
* release: id, name, version_number, version_type, game_versions, loaders, published,
  changelog?, files[], server_pack?, java?, recommended_memory_mb?
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from django.conf import settings
from django.core.cache import cache

from apps.core.errors import ApiError, ServiceUnavailable, UpstreamError
from apps.core.models import SystemSetting

log = logging.getLogger(__name__)

MODRINTH_API = "https://api.modrinth.com/v2"
CURSEFORGE_API = "https://api.curseforge.com"
FTB_API = "https://api.feed-the-beast.com/v1/modpacks/public"
CF_GAME_MINECRAFT = 432
CF_CLASS_MODPACKS = 4471
CF_CLASS_MODS = 6
CF_CLASS_PLUGINS = 5
CF_LOADERS = {1: "forge", 4: "fabric", 5: "quilt", 6: "neoforge"}
CF_LOADER_IDS = {v: k for k, v in CF_LOADERS.items()}
CF_RELEASE_TYPES = {1: "release", 2: "beta", 3: "alpha"}
MODPACK_LOADERS = ("fabric", "forge", "neoforge", "quilt")


def _request(method: str, url: str, *, headers: dict[str, str] | None = None, provider: str, **kwargs: Any) -> Any:
    hdrs = {"User-Agent": settings.MCPANEL_USER_AGENT, "Accept": "application/json", **(headers or {})}
    try:
        resp = httpx.request(method, url, headers=hdrs, timeout=20, follow_redirects=True, **kwargs)
    except httpx.HTTPError as exc:
        raise UpstreamError(f"Could not reach {provider}: {exc}", code="provider_unreachable") from exc
    if resp.status_code == 404:
        raise ApiError(f"Not found on {provider}", code="provider_not_found", status_code=404)
    if resp.status_code == 429:
        raise UpstreamError(f"{provider} rate limit reached; try again shortly", code="provider_rate_limited")
    if resp.status_code in (401, 403):
        raise UpstreamError(f"{provider} rejected the request (check the API key)", code="provider_auth")
    if resp.status_code >= 400:
        raise UpstreamError(f"{provider} returned HTTP {resp.status_code}", code="provider_error")
    if not resp.content:
        return None
    return resp.json()


def _cached(key: str, ttl: int, fn: Any) -> Any:
    hit = cache.get(key)
    if hit is not None:
        return hit
    value = fn()
    cache.set(key, value, ttl)
    return value


# =====================================================================================
# Modrinth
# =====================================================================================
class Modrinth:
    name = "modrinth"
    label = "Modrinth"

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return _request("GET", f"{MODRINTH_API}{path}", params=params, provider="Modrinth")

    @staticmethod
    def _project_from_hit(hit: dict[str, Any]) -> dict[str, Any]:
        cats = hit.get("categories") or []
        return {
            "provider": "modrinth",
            "id": hit.get("project_id"),
            "slug": hit.get("slug"),
            "title": hit.get("title"),
            "description": hit.get("description"),
            "author": hit.get("author"),
            "icon_url": hit.get("icon_url"),
            "downloads": hit.get("downloads"),
            "follows": hit.get("follows"),
            "categories": [c for c in cats if c not in MODPACK_LOADERS],
            "loaders": [c for c in cats if c in (*MODPACK_LOADERS, "paper", "purpur", "folia", "spigot", "bukkit")],
            "game_versions": hit.get("versions") or [],
            "updated": hit.get("date_modified"),
            "server_side": hit.get("server_side"),
            "client_side": hit.get("client_side"),
            "url": f"https://modrinth.com/{hit.get('project_type', 'project')}/{hit.get('slug')}",
            "license": hit.get("license"),
            "project_type": hit.get("project_type"),
        }

    def search(
        self,
        *,
        query: str = "",
        project_type: str = "modpack",
        loaders: list[str] | None = None,
        game_version: str | None = None,
        categories: list[str] | None = None,
        sort: str = "relevance",
        offset: int = 0,
        limit: int = 20,
        server_only: bool = True,
    ) -> dict[str, Any]:
        facets: list[list[str]] = [[f"project_type:{project_type}"]]
        if loaders:
            facets.append([f"categories:{lo}" for lo in loaders])
        if game_version:
            facets.append([f"versions:{game_version}"])
        for cat in categories or []:
            facets.append([f"categories:{cat}"])
        if server_only:
            facets.append(["server_side:required", "server_side:optional"])
        index = sort if sort in ("relevance", "downloads", "follows", "newest", "updated") else "relevance"
        params = {"query": query, "facets": json.dumps(facets), "index": index, "offset": offset, "limit": min(limit, 100)}
        data = _cached(f"mr:search:{hash(json.dumps(params, sort_keys=True))}", 120, lambda: self._get("/search", params))
        return {
            "total": data.get("total_hits", 0),
            "offset": offset,
            "limit": limit,
            "results": [self._project_from_hit(h) for h in data.get("hits", [])],
        }

    def project(self, project_id: str) -> dict[str, Any]:
        p = _cached(f"mr:project:{project_id}", 600, lambda: self._get(f"/project/{project_id}"))
        return {
            "provider": "modrinth",
            "id": p["id"],
            "slug": p.get("slug"),
            "title": p.get("title"),
            "description": p.get("description"),
            "body": p.get("body"),
            "icon_url": p.get("icon_url"),
            "downloads": p.get("downloads"),
            "follows": p.get("followers"),
            "categories": (p.get("categories") or []) + (p.get("additional_categories") or []),
            "loaders": p.get("loaders") or [],
            "game_versions": p.get("game_versions") or [],
            "updated": p.get("updated"),
            "published": p.get("published"),
            "server_side": p.get("server_side"),
            "client_side": p.get("client_side"),
            "url": f"https://modrinth.com/{p.get('project_type', 'project')}/{p.get('slug')}",
            "license": (p.get("license") or {}).get("id"),
            "source_url": p.get("source_url"),
            "issues_url": p.get("issues_url"),
            "project_type": p.get("project_type"),
            "gallery": [g.get("url") for g in (p.get("gallery") or [])][:8],
        }

    @staticmethod
    def _release(v: dict[str, Any]) -> dict[str, Any]:
        files = v.get("files") or []
        primary = next((f for f in files if f.get("primary")), files[0] if files else None)
        return {
            "provider": "modrinth",
            "id": v["id"],
            "project_id": v.get("project_id"),
            "name": v.get("name"),
            "version_number": v.get("version_number"),
            "version_type": v.get("version_type"),
            "game_versions": v.get("game_versions") or [],
            "loaders": v.get("loaders") or [],
            "published": v.get("date_published"),
            "downloads": v.get("downloads"),
            "changelog": v.get("changelog"),
            "file": (
                {
                    "filename": primary.get("filename"),
                    "url": primary.get("url"),
                    "size": primary.get("size"),
                    "sha1": (primary.get("hashes") or {}).get("sha1"),
                    "sha512": (primary.get("hashes") or {}).get("sha512"),
                }
                if primary
                else None
            ),
            "dependencies": [
                {
                    "project_id": d.get("project_id"),
                    "version_id": d.get("version_id"),
                    "type": d.get("dependency_type"),
                    "file_name": d.get("file_name"),
                }
                for d in (v.get("dependencies") or [])
                if d.get("dependency_type") != "embedded"
            ],
            "status": v.get("status"),
        }

    def releases(
        self,
        project_id: str,
        *,
        loaders: list[str] | None = None,
        game_versions: list[str] | None = None,
        include_changelog: bool = False,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"include_changelog": str(include_changelog).lower()}
        if loaders:
            params["loaders"] = json.dumps(loaders)
        if game_versions:
            params["game_versions"] = json.dumps(game_versions)
        key = f"mr:versions:{project_id}:{hash(json.dumps(params, sort_keys=True))}"
        data = _cached(key, 300, lambda: self._get(f"/project/{project_id}/version", params))
        releases = [self._release(v) for v in data or [] if v.get("status") in (None, "listed", "archived")]
        releases.sort(key=lambda r: r.get("published") or "", reverse=True)
        return releases

    def release(self, version_id: str) -> dict[str, Any]:
        return self._release(_cached(f"mr:version:{version_id}", 600, lambda: self._get(f"/version/{version_id}")))

    def identify_files(self, sha1_hashes: list[str]) -> dict[str, dict[str, Any]]:
        if not sha1_hashes:
            return {}
        data = (
            _request(
                "POST", f"{MODRINTH_API}/version_files", json={"hashes": sha1_hashes, "algorithm": "sha1"}, provider="Modrinth"
            )
            or {}
        )
        return {h: self._release(v) for h, v in data.items()}

    def latest_for_files(self, sha1_hashes: list[str], loaders: list[str], game_versions: list[str]) -> dict[str, dict[str, Any]]:
        if not sha1_hashes:
            return {}
        body = {"hashes": sha1_hashes, "algorithm": "sha1", "loaders": loaders, "game_versions": game_versions}
        data = _request("POST", f"{MODRINTH_API}/version_files/update", json=body, provider="Modrinth") or {}
        return {h: self._release(v) for h, v in data.items()}

    def projects(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        data = self._get("/projects", {"ids": json.dumps(ids)}) or []
        return [
            {
                "id": p["id"],
                "slug": p.get("slug"),
                "title": p.get("title"),
                "icon_url": p.get("icon_url"),
                "server_side": p.get("server_side"),
                "client_side": p.get("client_side"),
                "project_type": p.get("project_type"),
            }
            for p in data
        ]

    def resolve_modpack(self, project_id: str, version_id: str) -> dict[str, Any]:
        project = self.project(project_id)
        release = self.release(version_id)
        if release.get("project_id") and release["project_id"] != project["id"]:
            raise ApiError("That release does not belong to the selected modpack", code="release_mismatch")
        if project.get("project_type") != "modpack":
            raise ApiError("The selected Modrinth project is not a modpack", code="not_a_modpack")
        loaders = [lo for lo in release["loaders"] if lo in MODPACK_LOADERS]
        if not loaders:
            raise ApiError("This release does not declare a supported server mod loader", code="unsupported_loader")
        if not release["game_versions"]:
            raise ApiError("This release does not declare a Minecraft version", code="unknown_minecraft_version")
        server_side = project.get("server_side")
        warnings = []
        if server_side == "unsupported":
            raise ApiError("This modpack is marked client-only on Modrinth and cannot run as a server", code="client_only")
        if len(release["game_versions"]) > 1:
            warnings.append(f"Release lists several Minecraft versions; using {release['game_versions'][0]}")
        return {
            "provider": "modrinth",
            "server_type": "MODRINTH",
            "project_id": project["id"],
            "slug": project["slug"],
            "name": project["title"],
            "icon_url": project.get("icon_url") or "",
            "version_id": release["id"],
            "version_name": release.get("version_number") or release.get("name"),
            "minecraft_version": release["game_versions"][0],
            "loader": loaders[0],
            "loader_version": "",
            "file": release.get("file"),
            "warnings": warnings,
            "recommended_memory_mb": None,
        }


# =====================================================================================
# CurseForge (requires an API key from console.curseforge.com)
# =====================================================================================
def curseforge_key() -> str:
    return SystemSetting.get_secret("curseforge_api_key") or settings.CURSEFORGE_API_KEY or ""


class CurseForge:
    name = "curseforge"
    label = "CurseForge"

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        key = curseforge_key()
        if not key:
            raise ServiceUnavailable(
                "Browsing CurseForge needs an API key. Add one in System settings, or deploy by entering a pack URL.",
                code="curseforge_key_missing",
            )
        return _request("GET", f"{CURSEFORGE_API}{path}", params=params, headers={"x-api-key": key}, provider="CurseForge")

    @staticmethod
    def _project(m: dict[str, Any]) -> dict[str, Any]:
        loaders = sorted({CF_LOADERS.get(i.get("modLoader")) for i in (m.get("latestFilesIndexes") or [])} - {None})
        versions = sorted(
            {i.get("gameVersion") for i in (m.get("latestFilesIndexes") or []) if i.get("gameVersion")}, reverse=True
        )
        return {
            "provider": "curseforge",
            "id": str(m["id"]),
            "slug": m.get("slug"),
            "title": m.get("name"),
            "description": m.get("summary"),
            "author": ", ".join(a.get("name", "") for a in (m.get("authors") or [])[:3]),
            "icon_url": (m.get("logo") or {}).get("thumbnailUrl") or (m.get("logo") or {}).get("url"),
            "downloads": m.get("downloadCount"),
            "follows": m.get("thumbsUpCount"),
            "categories": [c.get("name") for c in (m.get("categories") or [])],
            "loaders": loaders,
            "game_versions": versions,
            "updated": m.get("dateModified"),
            "url": (m.get("links") or {}).get("websiteUrl"),
            "allow_distribution": m.get("allowModDistribution"),
            "server_side": None,
        }

    def search(
        self,
        *,
        query: str = "",
        project_type: str = "modpack",
        loaders: list[str] | None = None,
        game_version: str | None = None,
        categories: list[str] | None = None,
        sort: str = "relevance",
        offset: int = 0,
        limit: int = 20,
        server_only: bool = True,
    ) -> dict[str, Any]:
        class_id = {"modpack": CF_CLASS_MODPACKS, "mod": CF_CLASS_MODS, "plugin": CF_CLASS_PLUGINS}.get(
            project_type, CF_CLASS_MODPACKS
        )
        sort_field = {"relevance": 1, "downloads": 6, "updated": 3, "newest": 11, "follows": 12}.get(sort, 2)
        params: dict[str, Any] = {
            "gameId": CF_GAME_MINECRAFT,
            "classId": class_id,
            "searchFilter": query,
            "sortField": sort_field,
            "sortOrder": "desc",
            "index": offset,
            "pageSize": min(limit, 50),
        }
        if game_version:
            params["gameVersion"] = game_version
        if loaders and loaders[0] in CF_LOADER_IDS:
            params["modLoaderType"] = CF_LOADER_IDS[loaders[0]]
        data = _cached(f"cf:search:{hash(json.dumps(params, sort_keys=True))}", 120, lambda: self._get("/v1/mods/search", params))
        pag = data.get("pagination") or {}
        return {
            "total": min(pag.get("totalCount", 0), 10000),
            "offset": offset,
            "limit": limit,
            "results": [self._project(m) for m in data.get("data", [])],
        }

    def project(self, project_id: str) -> dict[str, Any]:
        data = _cached(f"cf:project:{project_id}", 600, lambda: self._get(f"/v1/mods/{int(project_id)}"))
        return self._project(data["data"])

    @staticmethod
    def _release(f: dict[str, Any]) -> dict[str, Any]:
        tags = f.get("gameVersions") or []
        loaders = [t.lower() for t in tags if t.lower() in MODPACK_LOADERS]
        mc = [t for t in tags if re.match(r"^\d+\.\d+", t)]
        return {
            "provider": "curseforge",
            "id": str(f["id"]),
            "project_id": str(f.get("modId")),
            "name": f.get("displayName"),
            "version_number": f.get("displayName"),
            "version_type": CF_RELEASE_TYPES.get(f.get("releaseType"), "release"),
            "game_versions": mc,
            "loaders": loaders,
            "published": f.get("fileDate"),
            "downloads": f.get("downloadCount"),
            "is_server_pack": bool(f.get("isServerPack")),
            "server_pack_file_id": f.get("serverPackFileId"),
            "file": {
                "filename": f.get("fileName"),
                "url": f.get("downloadUrl"),
                "size": f.get("fileLength"),
                "sha1": next((h.get("value") for h in (f.get("hashes") or []) if h.get("algo") == 1), None),
            },
            "distribution_allowed": f.get("downloadUrl") is not None,
            "dependencies": [],
        }

    def releases(
        self,
        project_id: str,
        *,
        loaders: list[str] | None = None,
        game_versions: list[str] | None = None,
        include_changelog: bool = False,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"pageSize": 50, "index": 0}
        if game_versions:
            params["gameVersion"] = game_versions[0]
        if loaders and loaders[0] in CF_LOADER_IDS:
            params["modLoaderType"] = CF_LOADER_IDS[loaders[0]]
        data = _cached(
            f"cf:files:{project_id}:{hash(json.dumps(params, sort_keys=True))}",
            300,
            lambda: self._get(f"/v1/mods/{int(project_id)}/files", params),
        )
        releases = [self._release(f) for f in data.get("data", []) if not f.get("isServerPack")]
        releases.sort(key=lambda r: r.get("published") or "", reverse=True)
        return releases

    def release(self, project_id: str, file_id: str) -> dict[str, Any]:
        data = _cached(
            f"cf:file:{project_id}:{file_id}", 600, lambda: self._get(f"/v1/mods/{int(project_id)}/files/{int(file_id)}")
        )
        return self._release(data["data"])

    def changelog(self, project_id: str, file_id: str) -> str:
        data = self._get(f"/v1/mods/{int(project_id)}/files/{int(file_id)}/changelog")
        return (data or {}).get("data", "")

    def resolve_modpack(self, project_id: str, version_id: str) -> dict[str, Any]:
        project = self.project(project_id)
        release = self.release(project_id, version_id)
        if release.get("is_server_pack"):
            raise ApiError(
                "Select the regular modpack file, not its server pack (the installer needs the manifest)",
                code="server_pack_selected",
            )
        if not release["game_versions"]:
            raise ApiError("This file does not declare a Minecraft version", code="unknown_minecraft_version")
        warnings = []
        if project.get("allow_distribution") is False or not release.get("distribution_allowed"):
            warnings.append(
                "Some files may not be downloadable automatically because the author disallows "
                "third-party distribution. The console will list files to download manually."
            )
        return {
            "provider": "curseforge",
            "server_type": "AUTO_CURSEFORGE",
            "project_id": project["id"],
            "slug": project["slug"],
            "name": project["title"],
            "icon_url": project.get("icon_url") or "",
            "version_id": release["id"],
            "version_name": release.get("name"),
            "minecraft_version": release["game_versions"][0],
            "loader": (release["loaders"] or [""])[0],
            "loader_version": "",
            "warnings": warnings,
            "recommended_memory_mb": None,
        }


# =====================================================================================
# Feed The Beast (public API used by the itzg FTBA installer)
# =====================================================================================
class FTB:
    name = "ftb"
    label = "Feed The Beast"

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        data = _request("GET", f"{FTB_API}{path}", params=params, provider="Feed The Beast")
        if isinstance(data, dict) and data.get("status") == "error":
            raise ApiError(data.get("message") or "FTB API error", code="provider_error", status_code=404)
        return data

    def _pack(self, pack_id: int | str) -> dict[str, Any]:
        return _cached(f"ftb:pack:{pack_id}", 600, lambda: self._get(f"/modpack/{int(pack_id)}"))

    def _project(self, p: dict[str, Any]) -> dict[str, Any]:
        art = p.get("art") or []
        icon = next((a.get("url") for a in art if a.get("type") == "square"), None)
        versions = p.get("versions") or []
        mcs: set[str] = set()
        loaders: set[str] = set()
        for v in versions:
            for t in v.get("targets") or []:
                if t.get("type") == "game":
                    mcs.add(t.get("version"))
                elif t.get("type") == "modloader":
                    loaders.add(t.get("name"))
        return {
            "provider": "ftb",
            "id": str(p["id"]),
            "slug": p.get("slug") or str(p["id"]),
            "title": p.get("name"),
            "description": p.get("synopsis"),
            "body": p.get("description"),
            "author": ", ".join(a.get("name", "") for a in (p.get("authors") or [])[:3]),
            "icon_url": icon,
            "downloads": p.get("installs"),
            "follows": p.get("plays"),
            "categories": [t.get("name") for t in (p.get("tags") or [])],
            "loaders": sorted(loaders - {None}),
            "game_versions": sorted(mcs - {None}, reverse=True),
            "updated": max((v.get("updated") or 0 for v in versions), default=None),
            "url": f"https://www.feed-the-beast.com/modpacks/{p['id']}-{p.get('slug') or ''}",
            "server_side": "required",
        }

    def search(
        self,
        *,
        query: str = "",
        project_type: str = "modpack",
        loaders: list[str] | None = None,
        game_version: str | None = None,
        categories: list[str] | None = None,
        sort: str = "relevance",
        offset: int = 0,
        limit: int = 20,
        server_only: bool = True,
    ) -> dict[str, Any]:
        if project_type != "modpack":
            return {"total": 0, "offset": 0, "limit": limit, "results": []}
        if query:
            ids = _cached(f"ftb:search:{query}", 300, lambda: self._get("/modpack/search/50", {"term": query}))
        else:
            ids = _cached("ftb:popular", 600, lambda: self._get("/modpack/popular/installs/50"))
        pack_ids = (ids or {}).get("packs", [])
        page = pack_ids[offset : offset + limit]
        results = []
        for pid in page:
            try:
                proj = self._project(self._pack(pid))
            except ApiError:
                continue
            if loaders and not set(loaders) & set(proj["loaders"]):
                continue
            if game_version and game_version not in proj["game_versions"]:
                continue
            results.append(proj)
        return {"total": len(pack_ids), "offset": offset, "limit": limit, "results": results}

    def project(self, project_id: str) -> dict[str, Any]:
        return self._project(self._pack(project_id))

    @staticmethod
    def _release(pack_id: str, v: dict[str, Any]) -> dict[str, Any]:
        targets = v.get("targets") or []
        mc = [t.get("version") for t in targets if t.get("type") == "game"]
        loader = next((t for t in targets if t.get("type") == "modloader"), {})
        java = next((t.get("version") for t in targets if t.get("type") == "runtime"), None)
        specs = v.get("specs") or {}
        return {
            "provider": "ftb",
            "id": str(v["id"]),
            "project_id": str(pack_id),
            "name": v.get("name"),
            "version_number": v.get("name"),
            "version_type": v.get("type"),
            "game_versions": mc,
            "loaders": [loader.get("name")] if loader.get("name") else [],
            "loader_version": loader.get("version"),
            "published": v.get("updated"),
            "java": java,
            "recommended_memory_mb": specs.get("recommended"),
            "minimum_memory_mb": specs.get("minimum"),
            "dependencies": [],
        }

    def releases(self, project_id: str, **_: Any) -> list[dict[str, Any]]:
        pack = self._pack(project_id)
        releases = [self._release(project_id, v) for v in pack.get("versions") or [] if not v.get("private")]
        releases.sort(key=lambda r: r.get("published") or 0, reverse=True)
        return releases

    def resolve_modpack(self, project_id: str, version_id: str) -> dict[str, Any]:
        pack = self._pack(project_id)
        release = next(
            (self._release(project_id, v) for v in pack.get("versions") or [] if str(v["id"]) == str(version_id)), None
        )
        if release is None:
            raise ApiError("That FTB release was not found", code="release_not_found", status_code=404)
        if not release["game_versions"]:
            raise ApiError("This release does not declare a Minecraft version", code="unknown_minecraft_version")
        project = self._project(pack)
        warnings = []
        if release.get("version_type") == "archived":
            warnings.append("This release is archived by FTB")
        return {
            "provider": "ftb",
            "server_type": "FTBA",
            "project_id": project["id"],
            "slug": project["slug"],
            "name": project["title"],
            "icon_url": project.get("icon_url") or "",
            "version_id": release["id"],
            "version_name": release.get("name"),
            "minecraft_version": release["game_versions"][0],
            "loader": (release["loaders"] or [""])[0],
            "loader_version": release.get("loader_version") or "",
            "warnings": warnings,
            "recommended_memory_mb": release.get("recommended_memory_mb"),
            "java": release.get("java"),
        }


PROVIDERS: dict[str, Any] = {"modrinth": Modrinth(), "curseforge": CurseForge(), "ftb": FTB()}


def get_provider(name: str) -> Any:
    try:
        return PROVIDERS[name]
    except KeyError as exc:
        raise ApiError(f"Unknown provider {name!r}", code="unknown_provider") from exc
