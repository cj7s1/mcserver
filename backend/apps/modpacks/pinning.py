"""Pin the release that the runtime actually installed (for packs deployed without a pinned id).

A CurseForge pack deployed by URL or slug without an API key (``views.keyless_curseforge``) has
no project id and no mod loader, and without a file in the URL no release either. The itzg
runtime's install manifest (``/data/.curseforge-manifest.json``) has all three: the reconciler
calls :func:`pin_installed_release` once the server is installed, and the modpack page calls it
again (rate-limited) while something is still missing.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.core.cache import cache

from apps.core.errors import ApiError
from apps.instances.models import Instance, ServerType
from apps.nodes.client import AgentClient

from .providers import MODPACK_LOADERS

log = logging.getLogger(__name__)

# A retry of the manifest read (modpack page) at most this often per server.
RETRY_SECONDS = 60


def loader_from_manifest(mod_loader_id: Any) -> str:
    """'fabric' for the manifest's ``modLoaderId`` 'fabric-0.15.11' (forge, neoforge, quilt alike)."""
    name = str(mod_loader_id or "").strip().lower().split("-", 1)[0]
    return name if name in MODPACK_LOADERS else ""


def needs_pin(instance: Instance) -> bool:
    return instance.server_type == ServerType.AUTO_CURSEFORGE and not (
        instance.modpack_version_id and instance.modpack_project_id and instance.modpack_loader
    )


def pin_installed_release(instance: Instance) -> bool:
    """Read the itzg install manifest and record what is missing: the installed release (only when
    none is pinned), the project id, the pack name and the mod loader. Returns True if anything
    was recorded."""
    if not needs_pin(instance):
        return False
    try:
        raw = AgentClient(instance.node).read_text(instance.id, ".curseforge-manifest.json")["content"]
        manifest = json.loads(raw)
    except (ApiError, json.JSONDecodeError) as exc:
        log.info("no curseforge manifest for %s yet: %s", instance.pk, exc)
        return False
    if not isinstance(manifest, dict):
        return False
    file_id = str(manifest.get("fileId") or "")
    project_id = str(manifest.get("modId") or "")
    fields: list[str] = []
    pinned_now = False
    if not instance.modpack_version_id:
        if not file_id:
            return False
        instance.modpack_version_id = file_id
        instance.modpack_version_name = str(manifest.get("modpackVersion") or manifest.get("fileName") or "")[:200]
        if manifest.get("minecraftVersion"):
            instance.minecraft_version = str(manifest["minecraftVersion"])[:40]
        fields += ["modpack_version_id", "modpack_version_name", "minecraft_version"]
        pinned_now = True
    elif file_id and file_id != instance.modpack_version_id:
        # the manifest is of another release (an update not applied yet): only the pin is trusted
        log.info(
            "manifest of %s is for file %s, pinned is %s: not recording from it",
            instance.pk,
            file_id,
            instance.modpack_version_id,
        )
        return False
    if project_id.isdigit() and not instance.modpack_project_id:
        instance.modpack_project_id = project_id
        fields.append("modpack_project_id")
    if manifest.get("modpackName") and (pinned_now or not instance.modpack_name):
        instance.modpack_name = str(manifest["modpackName"])[:200]
        fields.append("modpack_name")
    loader = loader_from_manifest(manifest.get("modLoaderId"))
    if loader and not instance.modpack_loader:
        instance.modpack_loader = loader
        fields.append("modpack_loader")
    if not fields:
        return False
    meta = dict(instance.modpack_meta or {})
    meta["pinned_from_manifest"] = {k: manifest.get(k) for k in ("fileId", "fileName", "modId", "slug", "modLoaderId")}
    instance.modpack_meta = meta
    instance.save(update_fields=[*dict.fromkeys(fields), "modpack_meta"])
    if pinned_now:
        from apps.instances.services import bump_revision

        bump_revision(instance)
        log.info("pinned %s to CurseForge file %s", instance.pk, file_id)
    else:
        log.info("recorded %s of %s from its CurseForge manifest", ", ".join(fields), instance.pk)
    return True


def retry_pin(instance: Instance) -> bool:
    """:func:`pin_installed_release` for a page view, at most every RETRY_SECONDS per server."""
    if not needs_pin(instance) or not cache.add(f"modpack:pin-retry:{instance.pk}", 1, RETRY_SECONDS):
        return False
    try:
        return pin_installed_release(instance)
    except Exception as exc:  # best effort, like the reconciler's call
        log.warning("pinning failed for %s: %s", instance.pk, exc)
        return False
