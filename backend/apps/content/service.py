"""Mods/plugins content manager.

The panel never touches instance files directly: every read and write goes through
the node agent, which independently confines paths to the instance directory,
refuses symlinks, only downloads from allow-listed provider CDNs and verifies the
published file hash (see agent ``safefs.py`` / ``fetch.py``). This module adds the
*compatibility* layer on top:

* Plugin servers (Paper/Purpur/Folia) only get plugin builds; mod servers only get
  builds for their mod loader. Minecraft versions must match exactly.
* Files are identified on Modrinth by SHA-1 (``POST /version_files``); anything not
  found there is reported as a manually added file.
* Filenames supplied by clients must be a single path component ending in
  ``.jar`` or ``.jar.disabled``; the content directory is chosen by the server.
"""

from __future__ import annotations

import hashlib
import logging
import re
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from django.core.cache import cache

from apps.core.errors import ApiError, ConflictError, UpstreamError
from apps.instances.models import Instance, ServerType, Status
from apps.jobs.models import ACTIVE_STATUSES, Job
from apps.modpacks.providers import PROVIDERS
from apps.nodes.client import AgentClient, AgentError

log = logging.getLogger(__name__)

# Which Modrinth loader tags each server can run. Paper forks load Bukkit/Spigot/Paper
# plugins; Folia only loads plugins that explicitly support it. Quilt loads Fabric mods.
PLUGIN_LOADERS: dict[str, tuple[str, ...]] = {
    ServerType.PAPER: ("paper", "spigot", "bukkit"),
    ServerType.PURPUR: ("purpur", "paper", "spigot", "bukkit"),
    ServerType.FOLIA: ("folia",),
}
MOD_LOADERS: dict[str, tuple[str, ...]] = {
    "fabric": ("fabric",),
    "quilt": ("quilt", "fabric"),
    "forge": ("forge",),
    "neoforge": ("neoforge",),
}
PLUGIN_LOADER_TAGS = frozenset({"paper", "purpur", "folia", "spigot", "bukkit"})
MOD_LOADER_TAGS = frozenset(MOD_LOADERS)
LOADER_LABELS = {
    "paper": "Paper",
    "purpur": "Purpur",
    "folia": "Folia",
    "spigot": "Spigot",
    "bukkit": "Bukkit",
    "fabric": "Fabric",
    "quilt": "Quilt",
    "forge": "Forge",
    "neoforge": "NeoForge",
}
UNPINNED_VERSIONS = frozenset({"", "LATEST", "SNAPSHOT"})
MODRINTH_DOWNLOAD_HOSTS = frozenset({"cdn.modrinth.com", "cdn-raw.modrinth.com"})
RUNNING_STATUSES = frozenset({Status.RUNNING, Status.STARTING, Status.UNHEALTHY, Status.RESTARTING})
SEARCH_SORTS = ("relevance", "downloads", "follows", "newest", "updated")

ENABLED_SUFFIX = ".jar"
DISABLED_SUFFIX = ".jar.disabled"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_DEPENDENCY_DEPTH = 3
MAX_INSTALLS_PER_REQUEST = 25
MAX_FILES_PER_UPDATE = 100
HASH_BATCH = 500
PROJECT_BATCH = 100

IDENTIFY_TTL = 3600  # a hash -> version mapping never changes once published
IDENTIFY_MISS_TTL = 600  # unknown hashes may be published later
PROJECT_TTL = 3600
INSTALLED_TTL = 60

UNTRUSTED_UPLOAD_NOTICE = (
    "Uploaded jars are untrusted third-party code: they run inside the server with full access to its files "
    "and network. Only upload files from sources you trust."
)
MODPACK_WARNING = (
    "This server runs a modpack. Mods installed by the pack may be replaced, re-added or removed when the "
    "modpack is updated or re-synchronised, so changes to pack-managed files may not be permanent."
)
UNPINNED_WARNING = (
    "This server follows the newest Minecraft version, which can change on restart. Set a specific Minecraft "
    "version to search, install or update content with compatibility checks."
)

_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f/\\]")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
_VERSION_ID = re.compile(r"^[A-Za-z0-9]{1,32}$")
_FOLIA_SUPPORTED = re.compile(rb"^folia-supported:\s*['\"]?true['\"]?\s*$", re.MULTILINE | re.IGNORECASE)

# Files whose presence in a jar reveals which platform it targets.
PLATFORM_MARKERS = {
    "plugin.yml": "bukkit",
    "paper-plugin.yml": "paper",
    "fabric.mod.json": "fabric",
    "quilt.mod.json": "quilt",
    "META-INF/mods.toml": "forge",
    "mcmod.info": "forge",
    "META-INF/neoforge.mods.toml": "neoforge",
}
# Platforms whose jars a server can load (NeoForge for 1.20.1 still used META-INF/mods.toml).
PLATFORMS_ACCEPTED: dict[str, frozenset[str]] = {
    ServerType.PAPER: frozenset({"bukkit", "paper"}),
    ServerType.PURPUR: frozenset({"bukkit", "paper"}),
    ServerType.FOLIA: frozenset({"bukkit", "paper"}),
    "fabric": frozenset({"fabric"}),
    "quilt": frozenset({"quilt", "fabric"}),
    "forge": frozenset({"forge"}),
    "neoforge": frozenset({"neoforge", "forge"}),
}


def _mr() -> Any:
    return PROVIDERS["modrinth"]


# =====================================================================================
# Context and validation
# =====================================================================================
@dataclass(frozen=True)
class ContentContext:
    instance: Instance
    kind: str  # "plugins" | "mods"
    directory: str  # "plugins" | "mods"
    project_type: str  # Modrinth search project type: "plugin" | "mod"
    loaders: tuple[str, ...]
    minecraft_version: str | None  # None when the server follows LATEST/SNAPSHOT

    @property
    def platform(self) -> str:
        """Human label of what this server runs, e.g. 'Paper' or 'Fabric'."""
        if self.kind == "plugins":
            return ServerType(self.instance.server_type).label
        return LOADER_LABELS.get(self.loaders[0], self.loaders[0])

    @property
    def platform_key(self) -> str:
        return self.instance.server_type if self.kind == "plugins" else self.loaders[0]

    @property
    def noun(self) -> str:
        return "plugin" if self.kind == "plugins" else "mod"

    def path(self, filename: str) -> str:
        return f"{self.directory}/{filename}"

    @property
    def restart_required(self) -> bool:
        return self.instance.status in RUNNING_STATUSES


def context_for(instance: Instance, *, require_version: bool = True) -> ContentContext:
    kind = instance.content_kind
    if kind == "none":
        raise ConflictError("This server type does not support mods or plugins", code="content_unsupported")
    if kind == "plugins":
        loaders = PLUGIN_LOADERS[instance.server_type]
        directory, project_type = "plugins", "plugin"
    else:
        loader = (instance.effective_loader or "").strip().lower()
        if loader not in MOD_LOADERS:
            raise ConflictError(
                "Could not determine which mod loader this server uses, so compatible mods cannot be selected",
                code="loader_unknown",
                details={"loader": loader},
            )
        loaders = MOD_LOADERS[loader]
        directory, project_type = "mods", "mod"
    mc = (instance.minecraft_version or "").strip()
    pinned = mc if mc.upper() not in UNPINNED_VERSIONS else None
    if require_version and pinned is None:
        raise ConflictError(
            "This server follows the newest Minecraft version, which can change on restart, so mod/plugin "
            "compatibility cannot be guaranteed. Set a specific Minecraft version for this server first.",
            code="minecraft_version_unpinned",
        )
    return ContentContext(
        instance=instance,
        kind=kind,
        directory=directory,
        project_type=project_type,
        loaders=loaders,
        minecraft_version=pinned,
    )


def ensure_mutable(instance: Instance) -> None:
    """Refuse content changes while the server is archived or another operation holds its lock."""
    if instance.archived_at is not None:
        raise ConflictError("This server is archived; restore it before changing its content", code="instance_archived")
    if instance.active_job_id:
        job = Job.objects.filter(pk=instance.active_job_id, status__in=ACTIVE_STATUSES).first()
        if job is not None:
            raise ConflictError(
                f"Another operation is in progress: {job.title}",
                code="instance_busy",
                details={"job_id": str(job.id), "kind": job.kind},
            )


def validate_filename(name: Any, *, allow_disabled: bool = True) -> str:
    """A single path component ending in .jar (or .jar.disabled). Never strips or rewrites."""
    if not isinstance(name, str) or not name:
        raise ApiError("A file name is required", code="invalid_filename")
    if len(name.encode("utf-8", "surrogatepass")) > 255:
        raise ApiError("File name is too long", code="invalid_filename")
    if name in (".", "..") or _UNSAFE_NAME.search(name) or name != name.strip():
        raise ApiError("File name must be a single plain file name (no folders)", code="invalid_filename")
    if allow_disabled and name.endswith(DISABLED_SUFFIX):
        stem = name[: -len(DISABLED_SUFFIX)]
    elif name.endswith(ENABLED_SUFFIX):
        stem = name[: -len(ENABLED_SUFFIX)]
    else:
        allowed = "'.jar' or '.jar.disabled'" if allow_disabled else "'.jar'"
        raise ApiError(f"File name must end in {allowed}", code="invalid_filename")
    if not stem:
        raise ApiError("File name must have a name before the extension", code="invalid_filename")
    return name


def validate_project_id(value: Any) -> str:
    if not isinstance(value, str) or not _PROJECT_ID.match(value):
        raise ApiError("Invalid Modrinth project ID or slug", code="invalid_project_id")
    return value


def validate_version_id(value: Any) -> str:
    if not isinstance(value, str) or not _VERSION_ID.match(value):
        raise ApiError("Invalid Modrinth version ID", code="invalid_version_id")
    return value


def is_enabled(filename: str) -> bool:
    return filename.endswith(ENABLED_SUFFIX)


# =====================================================================================
# Modrinth lookups (cached)
# =====================================================================================
def _batches(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def identify(hashes: list[str]) -> dict[str, dict[str, Any]]:
    """Map SHA-1 -> Modrinth release for files Modrinth knows. Raises ApiError if Modrinth fails."""
    wanted = sorted({h.lower() for h in hashes if isinstance(h, str) and _SHA1.match(h.lower())})
    keys = {h: f"content:mr:sha1:{h}" for h in wanted}
    hits = cache.get_many(list(keys.values())) if keys else {}
    result: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for h, key in keys.items():
        if key in hits:
            if hits[key]:
                result[h] = hits[key]
        else:
            missing.append(h)
    for chunk in _batches(missing, HASH_BATCH):
        found = {k.lower(): v for k, v in _mr().identify_files(chunk).items()}
        cache.set_many({keys[h]: found[h] for h in chunk if h in found}, IDENTIFY_TTL)
        cache.set_many({keys[h]: {} for h in chunk if h not in found}, IDENTIFY_MISS_TTL)
        result.update({h: found[h] for h in chunk if h in found})
    return result


def project_summaries(ids: list[str]) -> dict[str, dict[str, Any]]:
    wanted = sorted({i for i in ids if i})
    keys = {i: f"content:mr:projlite:{i}" for i in wanted}
    hits = cache.get_many(list(keys.values())) if keys else {}
    result = {i: hits[k] for i, k in keys.items() if k in hits}
    missing = [i for i in wanted if i not in result]
    for chunk in _batches(missing, PROJECT_BATCH):
        for p in _mr().projects(chunk):
            summary = {
                "id": p["id"],
                "slug": p.get("slug"),
                "title": p.get("title"),
                "icon_url": p.get("icon_url"),
                "server_side": p.get("server_side"),
                "client_side": p.get("client_side"),
                "url": f"https://modrinth.com/project/{p.get('slug') or p['id']}",
            }
            result[p["id"]] = summary
            cache.set(keys.get(p["id"], f"content:mr:projlite:{p['id']}"), summary, PROJECT_TTL)
    return result


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_newer(current: dict[str, Any], candidate: dict[str, Any] | None) -> bool:
    if not candidate or candidate.get("id") == current.get("id"):
        return False
    cur_ts, new_ts = _ts(current.get("published")), _ts(candidate.get("published"))
    if cur_ts and new_ts:
        return new_ts > cur_ts
    return True


def _version_out(release: dict[str, Any]) -> dict[str, Any]:
    file = release.get("file") or {}
    return {
        "id": release.get("id"),
        "project_id": release.get("project_id"),
        "name": release.get("name"),
        "version_number": release.get("version_number"),
        "version_type": release.get("version_type"),
        "loaders": release.get("loaders") or [],
        "game_versions": release.get("game_versions") or [],
        "published": release.get("published"),
        "downloads": release.get("downloads"),
        "file": {"filename": file.get("filename"), "size": file.get("size")} if file else None,
        "dependencies": [
            {"project_id": d.get("project_id"), "version_id": d.get("version_id"), "type": d.get("type")}
            for d in release.get("dependencies") or []
            if d.get("type") in ("required", "optional", "incompatible")
        ],
    }


def _project_out(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": project.get("id"),
        "slug": project.get("slug"),
        "title": project.get("title"),
        "description": project.get("description"),
        "icon_url": project.get("icon_url"),
        "url": project.get("url"),
        "server_side": project.get("server_side"),
        "client_side": project.get("client_side"),
        "loaders": project.get("loaders") or [],
        "license": project.get("license"),
    }


def _label_list(tags: list[str] | tuple[str, ...]) -> str:
    return ", ".join(LOADER_LABELS.get(t, t) for t in tags) or "no declared loader"


def _mc_summary(versions: list[str]) -> str:
    if not versions:
        return "no declared Minecraft version"
    shown = versions[-4:]
    return ("…, " if len(versions) > 4 else "") + ", ".join(shown)


def incompatibility(ctx: ContentContext, release: dict[str, Any]) -> tuple[str, str] | None:
    """Return (code, message) explaining why a release cannot be installed here, or None."""
    name = release.get("version_number") or release.get("name") or release.get("id")
    loaders = [str(lo).lower() for lo in release.get("loaders") or []]
    if not set(loaders) & set(ctx.loaders):
        if ctx.kind == "plugins" and set(loaders) & MOD_LOADER_TAGS:
            msg = (
                f"Version {name} is a mod for {_label_list(loaders)}, but this {ctx.platform} server runs plugins "
                f"({_label_list(ctx.loaders)})"
            )
        elif ctx.kind == "mods" and set(loaders) & PLUGIN_LOADER_TAGS:
            msg = f"Version {name} is a server plugin for {_label_list(loaders)}, but this server runs {ctx.platform} mods"
        else:
            msg = f"Version {name} is for {_label_list(loaders)}, but this server needs {_label_list(ctx.loaders)}"
        return "incompatible_loader", msg
    game_versions = release.get("game_versions") or []
    if ctx.minecraft_version and ctx.minecraft_version not in game_versions:
        return (
            "incompatible_minecraft_version",
            f"Version {name} supports Minecraft {_mc_summary(game_versions)}, not {ctx.minecraft_version}",
        )
    file = release.get("file") or {}
    filename = file.get("filename") or ""
    try:
        validate_filename(filename, allow_disabled=False)
    except ApiError:
        return "no_installable_file", f"Version {name} does not provide a .jar file that can be installed"
    parsed = urlparse(file.get("url") or "")
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in MODRINTH_DOWNLOAD_HOSTS:
        return "no_installable_file", f"Version {name} is not downloadable from Modrinth's CDN"
    if not (file.get("sha1") or file.get("sha512")):
        return "no_installable_file", f"Version {name} has no published file hash to verify the download"
    return None


def installable_project(ctx: ContentContext, project_id: str) -> dict[str, Any]:
    project = _mr().project(validate_project_id(project_id))
    title = project.get("title") or project_id
    ptype = project.get("project_type")
    if ptype not in ("mod", "plugin"):
        raise ApiError(f"{title} is a {ptype or 'project'}, not a {ctx.noun}", code="not_installable")
    if project.get("server_side") == "unsupported":
        raise ApiError(f"{title} is client-only and does not run on servers", code="client_only")
    project_loaders = [str(lo).lower() for lo in project.get("loaders") or []]
    if project_loaders and not set(project_loaders) & set(ctx.loaders):
        if ctx.kind == "plugins" and not set(project_loaders) & PLUGIN_LOADER_TAGS:
            msg = f"{title} is a mod ({_label_list(project_loaders)}), not a plugin; this {ctx.platform} server runs plugins"
        elif ctx.kind == "mods" and not set(project_loaders) & MOD_LOADER_TAGS:
            msg = f"{title} is a server plugin ({_label_list(project_loaders)}), not a mod for {ctx.platform}"
        else:
            msg = f"{title} is not available for {_label_list(ctx.loaders)} (it supports {_label_list(project_loaders)})"
        raise ApiError(msg, code="incompatible_loader")
    return project


def compatible_releases(ctx: ContentContext, project_id: str) -> list[dict[str, Any]]:
    releases = _mr().releases(project_id, loaders=list(ctx.loaders), game_versions=[ctx.minecraft_version])
    # re-check locally: never trust upstream filtering alone for what gets installed
    return [r for r in releases if incompatibility(ctx, r) is None]


def _no_compatible(ctx: ContentContext, project: dict[str, Any]) -> ApiError:
    return ApiError(
        f"{project.get('title') or project.get('id')} has no version for {ctx.platform} on Minecraft {ctx.minecraft_version}",
        code="no_compatible_version",
    )


def select_release(ctx: ContentContext, project: dict[str, Any], version_id: str | None) -> dict[str, Any]:
    if version_id:
        release = _mr().release(validate_version_id(version_id))
        if release.get("project_id") != project["id"]:
            raise ApiError(f"That version does not belong to {project.get('title')}", code="version_mismatch")
        problem = incompatibility(ctx, release)
        if problem:
            raise ApiError(problem[1], code=problem[0])
        return release
    releases = compatible_releases(ctx, project["id"])
    if not releases:
        raise _no_compatible(ctx, project)
    stable = [r for r in releases if r.get("version_type") == "release"]
    if not stable:
        raise ApiError(
            f"{project.get('title')} only has beta/alpha versions for {ctx.platform} {ctx.minecraft_version}; "
            "choose a version explicitly to install one",
            code="no_stable_release",
            details={"versions": [r["id"] for r in releases[:10]]},
        )
    return stable[0]


def _select_dependency_release(ctx: ContentContext, project: dict[str, Any], pinned: str | None) -> dict[str, Any]:
    releases = compatible_releases(ctx, project["id"])
    stable = [r for r in releases if r.get("version_type") == "release"]
    if stable:
        return stable[0]
    if pinned and _VERSION_ID.match(pinned):
        try:
            release = _mr().release(pinned)
        except ApiError:
            release = None
        if release and release.get("project_id") == project["id"] and incompatibility(ctx, release) is None:
            return release
    if releases:
        return releases[0]
    raise _no_compatible(ctx, project)


# =====================================================================================
# Local inventory
# =====================================================================================
@dataclass(frozen=True)
class LocalFile:
    filename: str
    enabled: bool
    size: int
    modified: float | None
    sha1: str


def scan(ctx: ContentContext, client: AgentClient) -> list[LocalFile]:
    entries = client.hash_files(ctx.instance.id, ctx.directory, "sha1")
    out: list[LocalFile] = []
    for e in entries:
        name = e.get("name")
        digest = str(e.get("hash") or "").lower()
        if e.get("type") != "file" or not isinstance(name, str) or not _SHA1.match(digest):
            continue
        if not (name.endswith(ENABLED_SUFFIX) or name.endswith(DISABLED_SUFFIX)):
            continue
        out.append(
            LocalFile(
                filename=name, enabled=is_enabled(name), size=int(e.get("size") or 0), modified=e.get("modified"), sha1=digest
            )
        )
    out.sort(key=lambda f: f.filename.lower())
    return out


def _installed_key(instance: Instance) -> str:
    return f"content:installed:{instance.pk}"


def invalidate(instance: Instance) -> None:
    cache.delete(_installed_key(instance))


def _remember_installed(instance: Instance, versions: dict[str, dict[str, Any]]) -> set[str]:
    ids = {v["project_id"] for v in versions.values() if v.get("project_id")}
    cache.set(_installed_key(instance), sorted(ids), INSTALLED_TTL)
    return ids


def installed_project_ids(ctx: ContentContext, client: AgentClient) -> set[str]:
    hit = cache.get(_installed_key(ctx.instance))
    if hit is not None:
        return set(hit)
    files = scan(ctx, client)
    return _remember_installed(ctx.instance, identify([f.sha1 for f in files]))


def base_warnings(ctx: ContentContext) -> list[str]:
    warnings = []
    if ctx.instance.is_modpack:
        warnings.append(MODPACK_WARNING)
    if ctx.minecraft_version is None:
        warnings.append(UNPINNED_WARNING)
    return warnings


def inventory(instance: Instance) -> dict[str, Any]:
    ctx = context_for(instance, require_version=False)
    files = scan(ctx, AgentClient(instance.node))
    warnings = base_warnings(ctx)
    identification_error: str | None = None
    versions: dict[str, dict[str, Any]] = {}
    projects: dict[str, dict[str, Any]] = {}
    try:
        versions = identify([f.sha1 for f in files])
        projects = project_summaries([v.get("project_id") for v in versions.values()])
    except ApiError as exc:
        identification_error = str(exc.detail)
        warnings.append(f"Could not identify files on Modrinth: {identification_error}")
    if identification_error is None:
        _remember_installed(instance, versions)

    items = []
    enabled_by_project: dict[str, list[str]] = {}
    for f in files:
        release = versions.get(f.sha1)
        item: dict[str, Any] = {
            "filename": f.filename,
            "enabled": f.enabled,
            "size": f.size,
            "modified": f.modified,
            "sha1": f.sha1,
            "source": "modrinth" if release else ("manual" if identification_error is None else "unknown"),
            "project": None,
            "version": None,
            "compatibility": None,
        }
        if release:
            pid = release.get("project_id")
            item["project"] = projects.get(pid) or {"id": pid, "slug": None, "title": None, "icon_url": None, "url": None}
            item["version"] = _version_out(release)
            rel_loaders = {str(lo).lower() for lo in release.get("loaders") or []}
            item["compatibility"] = {
                "loader": bool(rel_loaders & set(ctx.loaders)),
                "minecraft": (ctx.minecraft_version in (release.get("game_versions") or [])) if ctx.minecraft_version else None,
            }
            if f.enabled and pid:
                enabled_by_project.setdefault(pid, []).append(f.filename)
        items.append(item)

    for pid, names in enabled_by_project.items():
        if len(names) > 1:
            title = (projects.get(pid) or {}).get("title") or pid
            warnings.append(f"Several enabled files belong to {title} ({', '.join(names)}); keep only one of them.")

    identified = sum(1 for i in items if i["source"] == "modrinth")
    return {
        "kind": ctx.kind,
        "directory": ctx.directory,
        "platform": ctx.platform,
        "loaders": list(ctx.loaders),
        "minecraft_version": ctx.minecraft_version,
        "is_modpack": instance.is_modpack,
        "restart_required_for_changes": ctx.restart_required,
        "identification": {"available": identification_error is None, "error": identification_error},
        "warnings": warnings,
        "summary": {
            "total": len(items),
            "enabled": sum(1 for i in items if i["enabled"]),
            "disabled": sum(1 for i in items if not i["enabled"]),
            "identified": identified,
            "manual": sum(1 for i in items if i["source"] == "manual"),
        },
        "files": items,
    }


# =====================================================================================
# Updates / search / versions
# =====================================================================================
def _latest_versions(ctx: ContentContext, current: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Latest compatible release per hash. Stable installs only move to stable releases."""
    stable = sorted(h for h, v in current.items() if v.get("version_type") == "release")
    pre = sorted(h for h, v in current.items() if v.get("version_type") != "release")
    out: dict[str, dict[str, Any]] = {}
    for hashes, types in ((stable, ["release"]), (pre, None)):
        for chunk in _batches(hashes, HASH_BATCH):
            found = _mr().latest_for_files(chunk, list(ctx.loaders), [ctx.minecraft_version], version_types=types)
            out.update({k.lower(): v for k, v in found.items()})
    return out


def check_updates(instance: Instance) -> dict[str, Any]:
    ctx = context_for(instance)
    files = [f for f in scan(ctx, AgentClient(instance.node)) if f.enabled]
    current = identify([f.sha1 for f in files])
    latest = _latest_versions(ctx, {f.sha1: current[f.sha1] for f in files if f.sha1 in current})
    projects = project_summaries([v.get("project_id") for v in current.values()])
    updates = []
    for f in files:
        cur = current.get(f.sha1)
        new = latest.get(f.sha1)
        if not cur or not _is_newer(cur, new) or incompatibility(ctx, new) is not None:
            continue
        updates.append(
            {
                "filename": f.filename,
                "project": projects.get(cur.get("project_id")) or {"id": cur.get("project_id")},
                "current": _version_out(cur),
                "latest": _version_out(new),
            }
        )
    return {
        "minecraft_version": ctx.minecraft_version,
        "loaders": list(ctx.loaders),
        "checked": sum(1 for f in files if f.sha1 in current),
        "unidentified": sum(1 for f in files if f.sha1 not in current),
        "updates": updates,
        "warnings": base_warnings(ctx),
    }


def search(instance: Instance, *, query: str, offset: int, limit: int, sort: str) -> dict[str, Any]:
    ctx = context_for(instance)
    result = _mr().search(
        query=query,
        project_type=ctx.project_type,
        loaders=list(ctx.loaders),
        game_version=ctx.minecraft_version,
        sort=sort if sort in SEARCH_SORTS else "relevance",
        offset=offset,
        limit=limit,
        server_only=True,
    )
    warnings = base_warnings(ctx)
    try:
        installed = installed_project_ids(ctx, AgentClient(instance.node))
    except ApiError as exc:
        installed = set()
        warnings.append(f"Could not check which projects are installed: {exc.detail}")
    for r in result["results"]:
        r["installed"] = r.get("id") in installed
    return {
        **result,
        "kind": ctx.kind,
        "project_type": ctx.project_type,
        "loaders": list(ctx.loaders),
        "minecraft_version": ctx.minecraft_version,
        "warnings": warnings,
    }


def project_versions(instance: Instance, project_id: str) -> dict[str, Any]:
    ctx = context_for(instance)
    project = installable_project(ctx, project_id)
    releases = compatible_releases(ctx, project["id"])
    try:
        installed = project["id"] in installed_project_ids(ctx, AgentClient(instance.node))
    except ApiError:
        installed = None
    return {
        "project": _project_out(project),
        "installed": installed,
        "minecraft_version": ctx.minecraft_version,
        "loaders": list(ctx.loaders),
        "versions": [_version_out(r) for r in releases],
    }


# =====================================================================================
# Mutations
# =====================================================================================
@dataclass
class _Planned:
    project: dict[str, Any]
    release: dict[str, Any]
    dependency: bool
    required_by: str | None

    @property
    def filename(self) -> str:
        return self.release["file"]["filename"]


def _installed_map(files: list[LocalFile], versions: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    by_project: dict[str, list[str]] = {}
    for f in files:
        release = versions.get(f.sha1)
        if release and release.get("project_id"):
            by_project.setdefault(release["project_id"], []).append(f.filename)
    return by_project


def _name_taken(filename: str, existing: set[str]) -> bool:
    return filename in existing or f"{filename}.disabled" in existing


def _fetch(client: AgentClient, ctx: ContentContext, release: dict[str, Any], dest: str, *, overwrite: bool) -> dict[str, Any]:
    file = release["file"]
    return client.fetch_file(
        ctx.instance.id,
        url=file["url"],
        path=ctx.path(dest),
        sha1=file.get("sha1"),
        sha512=file.get("sha512"),
        overwrite=overwrite,
    )


def _dep_key(entry: dict[str, Any]) -> str:
    return entry.get("project_id") or f"file:{entry.get('file_name')}"


def _missing_dependencies(
    releases: list[tuple[dict[str, Any], str]],
    satisfied: set[str],
    *,
    disabled: set[str] | None = None,
    skip: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Required dependencies of ``releases`` whose project is not enabled (``satisfied``)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set(skip or ())
    for release, title in releases:
        for dep in release.get("dependencies") or []:
            if dep.get("type") != "required":
                continue
            pid = dep.get("project_id")
            if not pid and dep.get("version_id"):
                try:
                    pid = _mr().release(validate_version_id(dep["version_id"])).get("project_id")
                except ApiError:
                    pid = None
            entry: dict[str, Any] = {
                "project_id": pid,
                "version_id": dep.get("version_id"),
                "file_name": dep.get("file_name"),
                "required_by": title,
                "reason": "not_installed",
                "message": None,
            }
            key = _dep_key(entry)
            if (pid and pid in satisfied) or key in seen:
                continue
            seen.add(key)
            if not pid:
                entry.update(
                    reason="not_on_modrinth", message="This dependency is not published on Modrinth; install it manually"
                )
            elif disabled and pid in disabled:
                entry.update(reason="disabled", message="Installed but disabled; enable it")
            out.append(entry)
    return out


def _annotate_titles(entries: list[dict[str, Any]]) -> None:
    ids = [e["project_id"] for e in entries if e.get("project_id")]
    if not ids:
        return
    try:
        summaries = project_summaries(ids)
    except ApiError:
        return
    for e in entries:
        s = summaries.get(e.get("project_id") or "")
        e["title"] = s.get("title") if s else None
        e["slug"] = s.get("slug") if s else None


def install(
    instance: Instance, *, project_id: str, version_id: str | None = None, install_dependencies: bool = False
) -> dict[str, Any]:
    ctx = context_for(instance)
    ensure_mutable(instance)
    client = AgentClient(instance.node)
    files = scan(ctx, client)
    try:
        versions = identify([f.sha1 for f in files])
    except ApiError as exc:
        raise UpstreamError(
            f"Could not check the installed {ctx.kind} on Modrinth: {exc.detail}", code="identification_failed"
        ) from exc
    installed = _installed_map(files, versions)
    enabled_ids = {v["project_id"] for f in files if f.enabled and (v := versions.get(f.sha1)) and v.get("project_id")}
    disabled_ids = set(installed) - enabled_ids
    existing = {f.filename for f in files}

    project = installable_project(ctx, project_id)
    title = project.get("title") or project["id"]
    if project["id"] in installed:
        raise ConflictError(
            f"{title} is already installed ({', '.join(installed[project['id']])}). Use update to change its version.",
            code="already_installed",
            details={"filenames": installed[project["id"]]},
        )
    root = _Planned(project, select_release(ctx, project, version_id), dependency=False, required_by=None)
    plan = [root]
    planned = {project["id"]}
    warnings: list[str] = []
    missing: list[dict[str, Any]] = []
    handled: set[str] = set()

    queue: list[tuple[_Planned, int]] = [(root, 1)]
    while queue:
        parent, depth = queue.pop(0)
        parent_title = parent.project.get("title") or parent.project["id"]
        for dep in parent.release.get("dependencies") or []:
            if dep.get("type") == "incompatible" and dep.get("project_id") in installed:
                warnings.append(
                    f"{parent_title} declares itself incompatible with an installed project "
                    f"({', '.join(installed[dep['project_id']])})"
                )
        entries = _missing_dependencies(
            [(parent.release, parent_title)], enabled_ids | planned, disabled=disabled_ids, skip=handled
        )
        for entry in entries:
            handled.add(_dep_key(entry))
            if entry["reason"] != "not_installed" or not install_dependencies:
                missing.append(entry)
                continue
            if depth > MAX_DEPENDENCY_DEPTH or len(plan) >= MAX_INSTALLS_PER_REQUEST:
                missing.append({**entry, "reason": "dependency_limit", "message": "Too many nested dependencies to install"})
                continue
            try:
                dep_project = installable_project(ctx, entry["project_id"])
                dep_release = _select_dependency_release(ctx, dep_project, entry.get("version_id"))
            except ApiError as exc:
                missing.append({**entry, "reason": exc.error_code, "message": str(exc.detail)})
                continue
            if dep_project["id"] in installed or dep_project["id"] in planned:
                continue
            item = _Planned(dep_project, dep_release, dependency=True, required_by=parent_title)
            if dep_release.get("version_type") != "release":
                warnings.append(
                    f"Dependency {dep_project.get('title')} has no stable release for this server; "
                    f"installing {dep_release.get('version_type')} {dep_release.get('version_number')}"
                )
            plan.append(item)
            planned.add(dep_project["id"])
            queue.append((item, depth + 1))

    names = [p.filename for p in plan]
    clashes = sorted({n for n in names if _name_taken(n, existing)} | {n for n in names if names.count(n) > 1})
    if clashes:
        raise ConflictError(
            f"A file with the same name already exists in {ctx.directory}/: {', '.join(clashes)}",
            code="file_exists",
            details={"filenames": clashes},
        )

    client.mkdir(instance.id, ctx.directory, parents=True)
    done: list[tuple[_Planned, dict[str, Any]]] = []
    try:
        for item in reversed(plan):  # dependencies first
            done.append((item, _fetch(client, ctx, item.release, item.filename, overwrite=False)))
    except ApiError:
        if done:
            try:
                client.delete_files(instance.id, [ctx.path(i.filename) for i, _ in done])
            except ApiError:
                log.exception("Could not roll back partially installed content on %s", instance.pk)
        raise
    finally:
        invalidate(instance)

    _annotate_titles(missing)
    return {
        "installed": [
            {
                "project_id": item.project["id"],
                "title": item.project.get("title"),
                "version_id": item.release["id"],
                "version_number": item.release.get("version_number"),
                "version_type": item.release.get("version_type"),
                "filename": item.filename,
                "size": result.get("size"),
                "dependency": item.dependency,
                "required_by": item.required_by,
            }
            for item, result in reversed(done)
        ],
        "missing_dependencies": missing,
        "warnings": warnings,
        "restart_required": ctx.restart_required,
    }


def update(instance: Instance, filenames: list[Any]) -> dict[str, Any]:
    ctx = context_for(instance)
    ensure_mutable(instance)
    if not isinstance(filenames, list) or not filenames:
        raise ApiError("Provide a list of file names to update", code="invalid_files")
    if len(filenames) > MAX_FILES_PER_UPDATE:
        raise ApiError(f"Update at most {MAX_FILES_PER_UPDATE} files at once", code="invalid_files")
    names = list(dict.fromkeys(validate_filename(n) for n in filenames))
    client = AgentClient(instance.node)
    files = scan(ctx, client)
    by_name = {f.filename: f for f in files}
    unknown = [n for n in names if n not in by_name]
    if unknown:
        raise ApiError(
            f"Not found in {ctx.directory}/: {', '.join(unknown)}",
            code="file_not_found",
            status_code=404,
            details={"filenames": unknown},
        )
    try:
        current = identify([f.sha1 for f in files])
    except ApiError as exc:
        raise UpstreamError(f"Could not identify files on Modrinth: {exc.detail}", code="identification_failed") from exc
    targets = [by_name[n] for n in names]
    latest = _latest_versions(ctx, {f.sha1: current[f.sha1] for f in targets if f.sha1 in current})
    existing = set(by_name)
    results: list[dict[str, Any]] = []
    updated_releases: list[tuple[dict[str, Any], str]] = []
    for f in targets:
        cur = current.get(f.sha1)
        base = {"filename": f.filename}
        if not cur:
            results.append(
                {**base, "status": "skipped", "reason": "manual", "message": "Not found on Modrinth; update it manually"}
            )
            continue
        new = latest.get(f.sha1)
        if new is None or not _is_newer(cur, new):
            results.append({**base, "status": "up_to_date", "version": _version_out(cur)})
            continue
        problem = incompatibility(ctx, new)
        if problem:
            results.append({**base, "status": "failed", "reason": problem[0], "message": problem[1]})
            continue
        new_name = new["file"]["filename"] + ("" if f.enabled else ".disabled")
        same = new_name == f.filename
        sibling = new_name[: -len(".disabled")] if new_name.endswith(DISABLED_SUFFIX) else f"{new_name}.disabled"
        clash = next((n for n in (new_name, sibling) if n in existing and n != f.filename), None)
        if clash:
            results.append({**base, "status": "failed", "reason": "file_exists", "message": f"{clash} already exists"})
            continue
        try:
            fetched = _fetch(client, ctx, new, new_name, overwrite=same)
        except ApiError as exc:
            results.append({**base, "status": "failed", "reason": exc.error_code, "message": str(exc.detail)})
            continue
        entry: dict[str, Any] = {
            **base,
            "status": "updated",
            "new_filename": new_name,
            "size": fetched.get("size"),
            "from_version": _version_out(cur),
            "to_version": _version_out(new),
        }
        existing.add(new_name)
        if not same:
            try:
                client.delete_files(instance.id, [ctx.path(f.filename)])
                existing.discard(f.filename)
            except ApiError as exc:
                entry["warning"] = f"The new version was installed but the old file could not be removed: {exc.detail}"
        results.append(entry)
        updated_releases.append((new, new.get("name") or new_name))
    invalidate(instance)

    enabled_ids = {v["project_id"] for f in files if f.enabled and (v := current.get(f.sha1)) and v.get("project_id")}
    enabled_ids |= {r["project_id"] for r, _ in updated_releases if r.get("project_id")}
    disabled_ids = {v["project_id"] for v in current.values() if v.get("project_id")} - enabled_ids
    missing = _missing_dependencies(updated_releases, enabled_ids, disabled=disabled_ids)
    _annotate_titles(missing)
    return {
        "results": results,
        "updated": sum(1 for r in results if r["status"] == "updated"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "missing_dependencies": missing,
        "restart_required": ctx.restart_required and any(r["status"] == "updated" for r in results),
    }


def _require_regular_file(ctx: ContentContext, client: AgentClient, filename: str) -> None:
    try:
        entry = client.stat_file(ctx.instance.id, ctx.path(filename))
    except AgentError as exc:
        if exc.agent_status == 404:
            raise ApiError(f"{filename} was not found in {ctx.directory}/", code="file_not_found", status_code=404) from exc
        raise
    if entry.get("type") != "file":
        raise ApiError(f"{filename} is not a regular file", code="not_a_file")


def toggle(instance: Instance, filename: Any, enabled: bool) -> dict[str, Any]:
    ctx = context_for(instance, require_version=False)
    ensure_mutable(instance)
    name = validate_filename(filename)
    client = AgentClient(instance.node)
    _require_regular_file(ctx, client, name)
    if is_enabled(name) == enabled:
        return {"filename": name, "previous_filename": name, "enabled": enabled, "changed": False, "restart_required": False}
    new = f"{name}.disabled" if is_enabled(name) else name[: -len(".disabled")]
    try:
        client.rename(instance.id, ctx.path(name), ctx.path(new), overwrite=False)
    except AgentError as exc:
        if exc.agent_status == 409:
            raise ConflictError(
                f"Cannot {'enable' if enabled else 'disable'} {name}: {new} already exists", code="file_exists"
            ) from exc
        raise
    finally:
        invalidate(instance)
    return {
        "filename": new,
        "previous_filename": name,
        "enabled": enabled,
        "changed": True,
        "restart_required": ctx.restart_required,
    }


def delete(instance: Instance, filename: Any) -> dict[str, Any]:
    ctx = context_for(instance, require_version=False)
    ensure_mutable(instance)
    name = validate_filename(filename)
    client = AgentClient(instance.node)
    _require_regular_file(ctx, client, name)
    try:
        client.delete_files(instance.id, [ctx.path(name)], recursive=False)
    finally:
        invalidate(instance)
    return {"deleted": name, "restart_required": ctx.restart_required and is_enabled(name)}


def _inspect_jar(fileobj: Any) -> tuple[set[str], bool | None]:
    """Detect target platforms from marker files (central directory only; nothing is extracted)."""
    fileobj.seek(0)
    try:
        with zipfile.ZipFile(fileobj) as zf:
            names = set(zf.namelist())
            platforms = {platform for marker, platform in PLATFORM_MARKERS.items() if marker in names}
            folia: bool | None = None
            for descriptor in ("plugin.yml", "paper-plugin.yml"):
                info = zf.getinfo(descriptor) if descriptor in names else None
                if info is not None and info.file_size <= 256 * 1024:
                    folia = bool(folia) or bool(_FOLIA_SUPPORTED.search(zf.read(info)))
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        EOFError,
        KeyError,
        ValueError,
        NotImplementedError,
        RuntimeError,
        zlib.error,
    ) as exc:
        raise ApiError("The file is not a valid .jar (zip) archive", code="invalid_jar") from exc
    finally:
        fileobj.seek(0)
    return platforms, folia


def _platform_warnings(ctx: ContentContext, platforms: set[str], folia: bool | None) -> list[str]:
    accepted = PLATFORMS_ACCEPTED.get(ctx.platform_key, frozenset())
    if not platforms:
        return [
            f"Could not detect which platform this jar targets (no plugin or mod descriptor found); "
            f"it may not load on this {ctx.platform} server."
        ]
    if not platforms & accepted:
        found = ", ".join(LOADER_LABELS.get(p, p) for p in sorted(platforms))
        kind = "plugin" if platforms <= {"bukkit", "paper"} else "mod"
        return [f"This jar looks like a {found} {kind}; this {ctx.platform} server will not load it."]
    if ctx.instance.server_type == ServerType.FOLIA and folia is False:
        return ["This plugin does not declare Folia support (folia-supported: true); Folia will refuse to load it."]
    return []


def upload(instance: Instance, uploaded: Any, *, overwrite: bool = False) -> dict[str, Any]:
    ctx = context_for(instance, require_version=False)
    ensure_mutable(instance)
    raw = str(getattr(uploaded, "name", "") or "")
    name = validate_filename(raw.replace("\\", "/").rsplit("/", 1)[-1], allow_disabled=False)
    size = getattr(uploaded, "size", None)
    if not size:
        raise ApiError("The uploaded file is empty", code="empty_file")
    if size > MAX_UPLOAD_BYTES:
        raise ApiError(f"Jar files may be at most {MAX_UPLOAD_BYTES // (1024 * 1024)} MB", code="file_too_large", status_code=413)
    platforms, folia = _inspect_jar(uploaded.file)
    warnings = _platform_warnings(ctx, platforms, folia)

    client = AgentClient(instance.node)
    sibling = f"{name}.disabled"
    try:
        client.stat_file(instance.id, ctx.path(sibling))
        warnings.append(f"A disabled copy ({sibling}) is also present; remove it if it is an older version.")
    except AgentError as exc:
        if exc.agent_status != 404:
            raise

    digest = hashlib.sha1()  # noqa: S324 - identification on Modrinth, not a security control

    def _chunks() -> Iterator[bytes]:
        for chunk in uploaded.chunks(256 * 1024):
            digest.update(chunk)
            yield chunk

    client.mkdir(instance.id, ctx.directory, parents=True)
    try:
        result = client.upload(instance.id, ctx.path(name), _chunks(), overwrite=overwrite)
    except AgentError as exc:
        if exc.agent_status == 409:
            raise ConflictError(
                f"{name} already exists in {ctx.directory}/; upload with overwrite to replace it", code="file_exists"
            ) from exc
        raise
    finally:
        invalidate(instance)

    sha1 = digest.hexdigest()
    identified = None
    try:
        release = identify([sha1]).get(sha1)
        if release:
            project = project_summaries([release.get("project_id")]).get(release.get("project_id")) or {}
            identified = {"project": project or {"id": release.get("project_id")}, "version": _version_out(release)}
            problem = incompatibility(ctx, release)
            # the jar inspection already explains a platform mismatch; only add what it cannot know
            if problem and problem[0] != "no_installable_file" and not (problem[0] == "incompatible_loader" and warnings):
                warnings.append(problem[1])
    except ApiError as exc:
        log.info("Could not identify uploaded jar %s: %s", name, exc)
    return {
        "uploaded": {"filename": name, "size": result.get("size", size), "sha1": sha1},
        "detected_platforms": sorted(platforms),
        "identified": identified,
        "warnings": warnings,
        "notice": UNTRUSTED_UPLOAD_NOTICE,
        "restart_required": ctx.restart_required,
    }
