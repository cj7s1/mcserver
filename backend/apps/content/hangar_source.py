"""Hangar as a second plugin source for Paper/Purpur/Folia servers.

Hangar has no hash lookup, so installs are recorded on the instance
(``modpack_meta["hangar"][filename]``) to label them in the inventory. Installing a plugin whose
recorded jar is still there replaces that jar (an update); records of jars that are gone are
dropped when the inventory is built and when the jar is deleted in the content manager.
"""

from __future__ import annotations

from typing import Any

from apps.core.errors import ApiError, ConflictError
from apps.instances.models import PLUGIN_TYPES, Instance
from apps.modpacks import hangar
from apps.nodes.client import AgentClient

from .packfiles import update_meta
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


def _set_record(instance: Instance, filename: str, record: dict[str, Any] | None, *, drop: tuple[str, ...] = ()) -> None:
    """Store ``record`` for ``filename`` (None: store nothing) after dropping the records ``drop``."""

    def change(meta: dict[str, Any]) -> None:
        hang = dict(meta.get("hangar") or {})
        for name in drop:
            hang.pop(name, None)
        if record is not None:
            hang[filename] = record
        meta["hangar"] = hang

    update_meta(instance, change)


def forget_files(instance: Instance, filenames: list[str]) -> None:
    """Drop the records of jars that are gone."""
    if any(f in recorded(instance) for f in filenames):
        _set_record(instance, "", None, drop=tuple(filenames))


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
    """Install a Hangar plugin; when the panel installed another version of it before and that jar
    is still there, this replaces it (the new jar is verified first, then the old one is deleted)."""
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
        listing = client.list_files(instance.id, "plugins")
        existing = {e["name"] for e in listing["entries"]}
    except ApiError:
        listing, existing = {}, set()
    if listing.get("truncated"):
        # (B-6: the listing was cut off: ask for the two names that matter instead of trusting it)
        for name in (filename, f"{filename}.disabled"):
            try:
                client.stat_file(instance.id, f"plugins/{name}")
                existing.add(name)
            except ApiError:
                pass
    # the jar the panel installed of this plugin before, if it is still there (enabled or disabled)
    previous = next(
        (
            name
            for name, rec in recorded(instance).items()
            if rec.get("slug") == slug and (name in existing or f"{name}.disabled" in existing)
        ),
        None,
    )
    if filename in existing or f"{filename}.disabled" in existing:
        raise ConflictError(
            f"{filename} is already installed" if previous in (None, filename) else f"{filename} already exists in plugins/",
            code="already_installed",
            details={"filenames": [filename]},
        )
    # an update keeps the old jar's state: a disabled plugin stays disabled
    disabled = previous is not None and previous not in existing
    target = f"{filename}.disabled" if disabled else filename
    client.mkdir(instance.id, "plugins", parents=True)
    result = client.fetch_file(instance.id, url=chosen["file"]["url"], path=f"plugins/{target}", sha256=chosen["file"]["sha256"])
    warnings: list[str] = []
    if previous is not None:
        old = f"{previous}.disabled" if disabled else previous
        try:
            client.delete_files(instance.id, [f"plugins/{old}"])
        except ApiError as exc:
            warnings.append(f"The new version was installed but the old file {old} could not be removed: {exc.detail}")
    previous_version = recorded(instance).get(previous, {}).get("version") if previous else None
    _set_record(instance, filename, {"slug": slug, "version": chosen["name"]}, drop=(previous,) if previous else ())
    invalidate(instance)
    deps = [d for d in chosen.get("dependencies") or [] if d.get("type") == "required"]
    return {
        "installed": {"filename": target, "slug": slug, "version": chosen["name"], "size": result.get("size")},
        # an update of a version installed before: the jar it replaced
        "replaced": {"filename": f"{previous}.disabled" if disabled else previous, "version": previous_version}
        if previous
        else None,
        "required_dependencies": deps,
        "warnings": warnings,
        "notes": [
            "Restart the server to load the plugin.",
            "Plugins are third-party code that runs with the server's full permissions.",
        ],
    }


def annotate_inventory(instance: Instance, inventory: dict[str, Any]) -> dict[str, Any]:
    rec = recorded(instance)
    if not rec:
        return inventory
    present = {item["filename"].removesuffix(".disabled") for item in inventory.get("files", [])}
    stale = tuple(name for name in rec if name not in present)
    if stale and inventory.get("files") is not None:
        # jars removed outside the content manager (file manager, by hand): forget them
        _set_record(instance, "", None, drop=stale)
        rec = {k: v for k, v in rec.items() if k not in stale}
    for item in inventory.get("files", []):
        base = item["filename"].removesuffix(".disabled")
        if item.get("source") in ("manual", "unknown", "too_large") and base in rec:  # not while still being hashed
            item["source"] = "hangar"
            item["hangar"] = rec[base]
    summary = inventory.get("summary") or {}
    summary["manual"] = sum(1 for i in inventory.get("files", []) if i.get("source") == "manual")
    summary["hangar"] = sum(1 for i in inventory.get("files", []) if i.get("source") == "hangar")
    return inventory
