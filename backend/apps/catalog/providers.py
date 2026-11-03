"""Server software version metadata from upstream sources (see docs/reference/version-metadata.md).

Every list is fetched from the upstream project's official metadata service and
cached. Nothing is invented: if an upstream is unreachable the API reports it.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

import httpx
from django.conf import settings
from django.core.cache import cache

from apps.core.errors import UpstreamError

log = logging.getLogger(__name__)

MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
FABRIC_META = "https://meta.fabricmc.net/v2"
QUILT_META = "https://meta.quiltmc.org/v3"
FORGE_PROMOS = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
FORGE_MAVEN_META = "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml"
NEOFORGE_VERSIONS = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"
NEOFORGE_LEGACY_VERSIONS = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/forge"
PAPER_FILL = "https://fill.papermc.io/v3/projects"
PURPUR_API = "https://api.purpurmc.org/v2/purpur"

JAVA_TAGS = (8, 11, 16, 17, 21, 25)


def _get(url: str, *, params: dict[str, Any] | None = None, as_text: bool = False, timeout: float = 20) -> Any:
    try:
        resp = httpx.get(
            url,
            params=params,
            headers={"User-Agent": settings.MCPANEL_USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise UpstreamError(f"Could not reach {httpx.URL(url).host}: {exc}", code="upstream_unreachable") from exc
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise UpstreamError(f"{httpx.URL(url).host} returned HTTP {resp.status_code}", code="upstream_error")
    return resp.text if as_text else resp.json()


def cached(key: str, ttl: int, fn: Any) -> Any:
    value = cache.get(key)
    if value is None:
        value = fn()
        if value is not None:
            cache.set(key, value, ttl)
    return value


# --- version ordering -----------------------------------------------------------
_num = re.compile(r"\d+")


def version_key(v: str) -> tuple:
    """Tolerant sort key for Minecraft-ish versions (1.20.1, 26.1.2, 47.4.20, 0.16.14)."""
    main = re.split(r"[-+]", v, maxsplit=1)[0]
    nums = tuple(int(n) for n in _num.findall(main))
    pre = 0 if re.search(r"(snapshot|pre|rc|beta|alpha)", v) else 1
    return (*nums, pre)


# --- Mojang -------------------------------------------------------------------------
def mojang_manifest() -> dict[str, Any]:
    return cached("catalog:mojang:manifest", 300, lambda: _get(MOJANG_MANIFEST))


def vanilla_versions(include_snapshots: bool = False) -> list[dict[str, Any]]:
    manifest = mojang_manifest()
    out = []
    for v in manifest.get("versions", []):
        if v["type"] == "release" or (include_snapshots and v["type"] == "snapshot"):
            out.append(
                {"version": v["id"], "type": v["type"], "released": v.get("releaseTime"), "stable": v["type"] == "release"}
            )
    return out


def latest_release() -> str:
    return mojang_manifest()["latest"]["release"]


def java_requirement(mc_version: str) -> int | None:
    """Mojang's declared Java major version for a Minecraft version (None if unknown)."""
    key = f"catalog:mojang:java:{mc_version}"
    cached_value = cache.get(key)
    if cached_value is not None:
        return cached_value
    manifest = mojang_manifest()
    if mc_version.upper() == "LATEST":
        mc_version = manifest["latest"]["release"]
    entry = next((v for v in manifest.get("versions", []) if v["id"] == mc_version), None)
    if entry is None:
        return None
    data = _get(entry["url"])
    major = (data or {}).get("javaVersion", {}).get("majorVersion")
    if major:
        cache.set(key, int(major), 60 * 60 * 24 * 30)
    return int(major) if major else None


def is_at_most(version: str, limit: str) -> bool:
    return version_key(version) <= version_key(limit)


def resolve_java(mc_version: str, server_type: str) -> dict[str, Any]:
    """Pick the itzg Java tag for a server.

    Rules (docs/reference/version-metadata.md §2): use Mojang's javaVersion and the
    smallest published image Java >= it; Forge <= 1.16.5 must run on Java 8.
    """
    required = java_requirement(mc_version)
    reason = "Mojang metadata"
    if required is None:
        required = 25 if not re.match(r"^1\.", mc_version or "") else 21
        reason = "Unknown version; assumed modern Java"
    java = next((t for t in JAVA_TAGS if t >= required), JAVA_TAGS[-1])
    if server_type == "FORGE" and re.match(r"^1\.\d+", mc_version) and is_at_most(mc_version, "1.16.5"):
        java = 8
        reason = "Forge 1.16.5 and older requires Java 8"
    return {"minecraft_version": mc_version, "required": required, "java_version": java, "reason": reason}


# --- Fabric / Quilt ---------------------------------------------------------------
def fabric_game_versions() -> list[dict[str, Any]]:
    data = cached("catalog:fabric:game", 1800, lambda: _get(f"{FABRIC_META}/versions/game"))
    return [{"version": v["version"], "stable": bool(v.get("stable"))} for v in data or []]


def fabric_loader_versions(mc_version: str) -> list[dict[str, Any]]:
    data = cached(f"catalog:fabric:loader:{mc_version}", 1800, lambda: _get(f"{FABRIC_META}/versions/loader/{mc_version}"))
    out = []
    for item in data or []:
        loader = item.get("loader", {})
        out.append({"version": loader.get("version"), "stable": bool(loader.get("stable"))})
    return out


def quilt_game_versions() -> list[dict[str, Any]]:
    data = cached("catalog:quilt:game", 1800, lambda: _get(f"{QUILT_META}/versions/game"))
    return [{"version": v["version"], "stable": bool(v.get("stable"))} for v in data or []]


def quilt_loader_versions(mc_version: str) -> list[dict[str, Any]]:
    data = cached(f"catalog:quilt:loader:{mc_version}", 1800, lambda: _get(f"{QUILT_META}/versions/loader/{mc_version}"))
    versions = {item.get("loader", {}).get("version") for item in data or []} - {None}
    ordered = sorted(versions, key=version_key, reverse=True)
    return [{"version": v, "stable": "beta" not in v} for v in ordered]


# --- Forge / NeoForge ---------------------------------------------------------------
def forge_promotions() -> dict[str, str]:
    data = cached("catalog:forge:promos", 1800, lambda: _get(FORGE_PROMOS))
    return (data or {}).get("promos", {})


def forge_game_versions() -> list[dict[str, Any]]:
    promos = forge_promotions()
    mcs = {k.rsplit("-", 1)[0] for k in promos}
    return [{"version": v, "stable": f"{v}-recommended" in promos} for v in sorted(mcs, key=version_key, reverse=True)]


def forge_loader_versions(mc_version: str) -> list[dict[str, Any]]:
    def _fetch() -> list[str]:
        text = _get(FORGE_MAVEN_META, as_text=True, timeout=40)
        if not text:
            return []
        root = ET.fromstring(text)  # noqa: S314 - trusted upstream over TLS, no entity expansion in stdlib parser
        return [el.text for el in root.iter("version") if el.text]

    all_versions = cached("catalog:forge:maven", 3600, _fetch)
    prefix = f"{mc_version}-"
    builds = []
    for full in all_versions:
        if full.startswith(prefix):
            rest = full[len(prefix) :]
            builds.append(rest.split("-")[0])
    promos = forge_promotions()
    recommended = promos.get(f"{mc_version}-recommended")
    latest = promos.get(f"{mc_version}-latest")
    out = []
    for b in sorted(set(builds), key=version_key, reverse=True):
        out.append({"version": b, "stable": b == recommended, "recommended": b == recommended, "latest": b == latest})
    return out


def _neoforge_mc(version: str) -> str | None:
    if version.startswith("0.") or "craftmine" in version:
        return None
    parts = version.split("-")[0].split(".")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if nums[0] >= 26 and len(nums) >= 4:  # Y.D.H.BUILD
        return f"{nums[0]}.{nums[1]}" if nums[2] == 0 else f"{nums[0]}.{nums[1]}.{nums[2]}"
    if 20 <= nums[0] < 26 and len(nums) >= 3:  # A.B.N -> 1.A.B
        return f"1.{nums[0]}" if nums[1] == 0 else f"1.{nums[0]}.{nums[1]}"
    return None


def neoforge_all() -> list[str]:
    data = cached("catalog:neoforge:versions", 1800, lambda: _get(NEOFORGE_VERSIONS))
    return list((data or {}).get("versions", []))


def neoforge_game_versions() -> list[dict[str, Any]]:
    mcs: dict[str, bool] = {}
    for v in neoforge_all():
        mc = _neoforge_mc(v)
        if mc and "alpha" not in v:
            mcs[mc] = mcs.get(mc, False) or ("beta" not in v)
    mcs.setdefault("1.20.1", True)  # legacy net.neoforged:forge artifact
    return [
        {"version": mc, "stable": stable} for mc, stable in sorted(mcs.items(), key=lambda kv: version_key(kv[0]), reverse=True)
    ]


def neoforge_loader_versions(mc_version: str) -> list[dict[str, Any]]:
    if mc_version == "1.20.1":
        data = cached("catalog:neoforge:legacy", 3600, lambda: _get(NEOFORGE_LEGACY_VERSIONS))
        versions = [v.split("-", 1)[1] for v in (data or {}).get("versions", []) if v.startswith("1.20.1-")]
    else:
        versions = [v for v in neoforge_all() if _neoforge_mc(v) == mc_version and "alpha" not in v]
    return [{"version": v, "stable": "beta" not in v} for v in sorted(set(versions), key=version_key, reverse=True)]


# --- Paper / Purpur / Folia -----------------------------------------------------------
def paper_versions(project: str = "paper") -> list[dict[str, Any]]:
    data = cached(f"catalog:fill:{project}:versions", 1800, lambda: _get(f"{PAPER_FILL}/{project}/versions"))
    out = []
    for item in (data or {}).get("versions", []):
        v = item.get("version", {})
        status = (v.get("support") or {}).get("status")
        out.append(
            {
                "version": v.get("id"),
                "stable": status == "SUPPORTED",
                "support": status,
                "java_minimum": ((v.get("java") or {}).get("version") or {}).get("minimum"),
            }
        )
    out.sort(key=lambda x: version_key(x["version"] or ""), reverse=True)
    return out


def paper_builds(mc_version: str, project: str = "paper") -> list[dict[str, Any]]:
    data = cached(
        f"catalog:fill:{project}:builds:{mc_version}", 900, lambda: _get(f"{PAPER_FILL}/{project}/versions/{mc_version}/builds")
    )
    return [
        {"version": str(b["id"]), "stable": b.get("channel") == "STABLE", "channel": b.get("channel"), "time": b.get("time")}
        for b in (data or [])
    ]


def purpur_versions() -> list[dict[str, Any]]:
    data = cached("catalog:purpur:versions", 3600, lambda: _get(PURPUR_API))
    versions = (data or {}).get("versions", [])
    return [{"version": v, "stable": True} for v in sorted(versions, key=version_key, reverse=True)]


def purpur_builds(mc_version: str) -> list[dict[str, Any]]:
    data = cached(f"catalog:purpur:builds:{mc_version}", 1800, lambda: _get(f"{PURPUR_API}/{mc_version}"))
    builds = ((data or {}).get("builds") or {}).get("all", [])
    return [{"version": b, "stable": True} for b in sorted(builds, key=lambda b: int(b) if b.isdigit() else 0, reverse=True)]


# --- dispatch ---------------------------------------------------------------------------
def game_versions(server_type: str, include_snapshots: bool = False) -> list[dict[str, Any]]:
    st = server_type.upper()
    if st == "VANILLA":
        return vanilla_versions(include_snapshots)
    if st == "FABRIC":
        items = fabric_game_versions()
    elif st == "QUILT":
        items = quilt_game_versions()
    elif st == "FORGE":
        items = forge_game_versions()
    elif st == "NEOFORGE":
        items = neoforge_game_versions()
    elif st == "PAPER":
        items = paper_versions("paper")
    elif st == "FOLIA":
        items = paper_versions("folia")
    elif st == "PURPUR":
        items = purpur_versions()
    else:
        return vanilla_versions(include_snapshots)
    if not include_snapshots:
        items = [i for i in items if not re.search(r"(snapshot|pre|rc|w\d+[a-z])", i["version"] or "")]
    return items


def loader_versions(server_type: str, mc_version: str) -> list[dict[str, Any]]:
    st = server_type.upper()
    if st == "FABRIC":
        return fabric_loader_versions(mc_version)
    if st == "QUILT":
        return quilt_loader_versions(mc_version)
    if st == "FORGE":
        return forge_loader_versions(mc_version)
    if st == "NEOFORGE":
        return neoforge_loader_versions(mc_version)
    if st == "PAPER":
        return paper_builds(mc_version, "paper")
    if st == "FOLIA":
        return paper_builds(mc_version, "folia")
    if st == "PURPUR":
        return purpur_builds(mc_version)
    return []


SERVER_TYPES: list[dict[str, Any]] = [
    {
        "id": "VANILLA",
        "label": "Vanilla",
        "category": "vanilla",
        "content": "none",
        "loader_label": None,
        "description": "The official Minecraft server from Mojang.",
    },
    {
        "id": "PAPER",
        "label": "Paper",
        "category": "plugins",
        "content": "plugins",
        "loader_label": "Paper build",
        "description": "High-performance Bukkit/Spigot-compatible server with plugin support.",
    },
    {
        "id": "PURPUR",
        "label": "Purpur",
        "category": "plugins",
        "content": "plugins",
        "loader_label": "Purpur build",
        "description": "Paper fork with extra gameplay configuration options.",
    },
    {
        "id": "FOLIA",
        "label": "Folia",
        "category": "plugins",
        "content": "plugins",
        "loader_label": "Folia build",
        "description": "Regionised multithreading fork of Paper (many plugins are incompatible).",
    },
    {
        "id": "FABRIC",
        "label": "Fabric",
        "category": "mods",
        "content": "mods",
        "loader_label": "Fabric loader",
        "description": "Lightweight mod loader. Most mods also need Fabric API.",
    },
    {
        "id": "QUILT",
        "label": "Quilt",
        "category": "mods",
        "content": "mods",
        "loader_label": "Quilt loader",
        "description": "Community fork of Fabric; loads most Fabric mods.",
    },
    {
        "id": "FORGE",
        "label": "Forge",
        "category": "mods",
        "content": "mods",
        "loader_label": "Forge version",
        "description": "The long-standing mod loader used by many large modpacks.",
    },
    {
        "id": "NEOFORGE",
        "label": "NeoForge",
        "category": "mods",
        "content": "mods",
        "loader_label": "NeoForge version",
        "description": "Modern fork of Forge used by most 1.20.2+ Forge-style mods.",
    },
]
