"""Hangar as a second plugin source for Paper/Purpur/Folia servers.

Hangar has no hash lookup, so installs are recorded on the instance
(``modpack_meta["hangar"][filename]``) to label them in the inventory.
"""

from __future__ import annotations

from typing import Any

from apps.core.errors import ApiError, ConflictError
from apps.instances.models import PLUGIN_TYPES, Instance
from apps.modpacks import hangar
from apps.nodes.client import AgentClient

from .service import ensure_mutable, invalidate, validate_filename


def _require_plugins(instance: Instance) -> None:
    if instance.server_type not in PLUGIN_TYPES:
        raise ApiError(
            "Hangar plugins are only available for Paper, Purpur and Folia servers", code="content_unsupported", status_code=409
        )
    if not instance.minecraft_version or instance.minecraft_version.upper() == "LATEST":
        raise ApiError(
            "Pin a Minecraft version first so compatible plugins can be chosen",
            code="minecraft_version_unpinned",
            status_code=409,
        )


def recorded(instance: Instance) -> dict[str, dict[str, Any]]:
    return dict((instance.modpack_meta or {}).get("hangar") or {})


def search(instance: Instance, query: str, offset: int = 0) -> dict[str, Any]:
    _require_plugins(instance)
    result = hangar.search(query, server_type=instance.server_type, minecraft_version=instance.minecraft_version, offset=offset)
    installed = {v.get("slug") for v in recorded(instance).values()}
    for p in result["results"]:
        p["installed"] = p.get("slug") in installed
    return result


def versions(instance: Instance, slug: str) -> dict[str, Any]:
    _require_plugins(instance)
    out = hangar.versions(slug, server_type=instance.server_type, minecraft_version=instance.minecraft_version)
    for v in out:
        f = v.get("file") or {}
        v["installable"] = bool(f.get("url") and f.get("sha256"))
        if not v["installable"]:
            v["reason"] = "Only available from an external site" if f.get("external_url") else "No downloadable file"
    return {"versions": out, "minecraft_version": instance.minecraft_version}


def install(instance: Instance, slug: str, version: str | None) -> dict[str, Any]:
    _require_plugins(instance)
    ensure_mutable(instance)
    candidates = versions(instance, slug)["versions"]
    if version:
        chosen = next((v for v in candidates if v["name"] == version), None)
        if chosen is None:
            raise ApiError(
                f"{slug} {version} is not available for Minecraft {instance.minecraft_version}",
                code="incompatible_minecraft_version",
            )
    else:
        chosen = next((v for v in candidates if v["version_type"] == "release" and v["installable"]), None)
        if chosen is None:
            raise ApiError(
                f"No installable release of {slug} for Minecraft {instance.minecraft_version}",
                code="no_stable_release",
                status_code=404,
            )
    if not chosen["installable"]:
        raise ApiError(f"{slug} {chosen['name']}: {chosen.get('reason')}", code="not_downloadable")
    filename = validate_filename(chosen["file"]["filename"], allow_disabled=False)
    client = AgentClient(instance.node)
    try:
        existing = {e["name"] for e in client.list_files(instance.id, "plugins")["entries"]}
    except ApiError:
        existing = set()
    if filename in existing or f"{filename}.disabled" in existing:
        raise ConflictError(f"{filename} is already installed", code="already_installed")
    client.mkdir(instance.id, "plugins", parents=True)
    result = client.fetch_file(
        instance.id, url=chosen["file"]["url"], path=f"plugins/{filename}", sha256=chosen["file"]["sha256"]
    )
    meta = dict(instance.modpack_meta or {})
    hang = dict(meta.get("hangar") or {})
    hang[filename] = {"slug": slug, "version": chosen["name"]}
    meta["hangar"] = hang
    instance.modpack_meta = meta
    instance.save(update_fields=["modpack_meta"])
    invalidate(instance)
    deps = [d for d in chosen.get("dependencies") or [] if d.get("type") == "required"]
    return {
        "installed": {"filename": filename, "slug": slug, "version": chosen["name"], "size": result.get("size")},
        "required_dependencies": deps,
        "notes": [
            "Restart the server to load the plugin.",
            "Plugins are third-party code that runs with the server's full permissions.",
        ],
    }


def annotate_inventory(instance: Instance, inventory: dict[str, Any]) -> dict[str, Any]:
    rec = recorded(instance)
    if not rec:
        return inventory
    for item in inventory.get("files", []):
        base = item["filename"].removesuffix(".disabled")
        if item.get("source") in ("manual", "unknown") and base in rec:  # not while still being hashed
            item["source"] = "hangar"
            item["hangar"] = rec[base]
    summary = inventory.get("summary") or {}
    summary["manual"] = sum(1 for i in inventory.get("files", []) if i.get("source") == "manual")
    summary["hangar"] = sum(1 for i in inventory.get("files", []) if i.get("source") == "hangar")
    return inventory
