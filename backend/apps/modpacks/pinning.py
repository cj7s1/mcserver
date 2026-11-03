"""Pin the release that the runtime actually installed (for packs deployed without a pinned id)."""

from __future__ import annotations

import json
import logging

from apps.core.errors import ApiError
from apps.instances.models import Instance, ServerType
from apps.nodes.client import AgentClient

log = logging.getLogger(__name__)


def pin_installed_release(instance: Instance) -> bool:
    """Read the itzg install manifest and store its release id. Returns True if a pin was added."""
    if instance.server_type != ServerType.AUTO_CURSEFORGE or instance.modpack_version_id:
        return False
    try:
        raw = AgentClient(instance.node).read_text(instance.id, ".curseforge-manifest.json")["content"]
        manifest = json.loads(raw)
    except (ApiError, json.JSONDecodeError) as exc:
        log.info("no curseforge manifest for %s yet: %s", instance.pk, exc)
        return False
    file_id = manifest.get("fileId")
    if not file_id:
        return False
    instance.modpack_version_id = str(file_id)
    instance.modpack_project_id = str(manifest.get("modId") or instance.modpack_project_id)
    instance.modpack_name = str(manifest.get("modpackName") or instance.modpack_name)[:200]
    instance.modpack_version_name = str(manifest.get("modpackVersion") or manifest.get("fileName") or "")[:200]
    if manifest.get("minecraftVersion"):
        instance.minecraft_version = str(manifest["minecraftVersion"])[:40]
    meta = dict(instance.modpack_meta or {})
    meta["pinned_from_manifest"] = {k: manifest.get(k) for k in ("fileId", "fileName", "modId", "slug", "modLoaderId")}
    instance.modpack_meta = meta
    instance.save(
        update_fields=[
            "modpack_version_id",
            "modpack_project_id",
            "modpack_name",
            "modpack_version_name",
            "minecraft_version",
            "modpack_meta",
        ]
    )
    from apps.instances.services import bump_revision

    bump_revision(instance)
    log.info("pinned %s to CurseForge file %s", instance.pk, file_id)
    return True
