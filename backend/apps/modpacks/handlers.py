"""Explicit modpack update and rollback jobs.

Modpacks are pinned to a provider release. Updating is an explicit, audited job:

1. resolve the target release with the provider (same project only),
2. take a pre-update backup (default on; the backup records the previous pin),
3. stop the server, change the pin (and Java/image if the Minecraft version requires it),
4. recreate the container so itzg installs the new release (it removes files that belonged
   to the old release using its manifest and keeps world data), and start it again if it
   was running.

Rollback restores the pre-update backup (files) and the previous pin together, because
running an older pack against a world touched by a newer one is not safe in general.
"""

from __future__ import annotations

from typing import Any

from apps import games
from apps.backups.handlers import RESTORE_UNTOUCHED, require_local_backup, run_backup
from apps.backups.models import Backup, BackupKind, BackupStatus
from apps.catalog.providers import resolve_java
from apps.core.errors import ApiError
from apps.core.models import SystemSetting
from apps.instances.handlers import Progress, _apply_refused, apply_container, ensure_stopped, put_back, starting_again
from apps.instances.models import Instance
from apps.instances.spec import image_for
from apps.jobs.models import Job
from apps.jobs.runner import JobContext, register
from apps.nodes.client import AgentClient

from .providers import get_provider

PIN_FIELDS = (
    "modpack_version_id",
    "modpack_version_name",
    "minecraft_version",
    "modpack_loader",
    "loader_version",
    "java_version",
    "image",
)


def current_pin(instance: Instance) -> dict[str, Any]:
    return {f: getattr(instance, f) for f in PIN_FIELDS}


def plan_update(instance: Instance, version_id: str) -> dict[str, Any]:
    """Resolve a target release and describe the change (no side effects)."""
    if "modpacks" not in games.capabilities(instance) or not instance.modpack_provider:
        raise ApiError("This server is not a modpack server", code="not_modpack", status_code=409)
    resolved = get_provider(instance.modpack_provider).resolve_modpack(instance.modpack_project_id, version_id)
    if str(resolved["project_id"]) != str(instance.modpack_project_id):
        raise ApiError("The release belongs to a different modpack", code="release_mismatch")
    java = resolve_java(resolved["minecraft_version"], "FORGE" if resolved.get("loader") == "forge" else "")
    warnings = list(resolved.get("warnings") or [])
    mc_changed = resolved["minecraft_version"] != instance.minecraft_version
    loader_changed = (resolved.get("loader") or "") != (instance.modpack_loader or "")
    if mc_changed:
        warnings.append(
            f"Minecraft version changes from {instance.minecraft_version} to {resolved['minecraft_version']}. "
            "Worlds are upgraded on first start and cannot be opened by older versions afterwards."
        )
    if loader_changed:
        warnings.append(f"Mod loader changes from {instance.modpack_loader or 'unknown'} to {resolved.get('loader')}.")
    java_version = max(java["java_version"], instance.java_version) if not mc_changed else java["java_version"]
    return {
        "current": current_pin(instance),
        "target": {
            "modpack_version_id": resolved["version_id"],
            "modpack_version_name": resolved.get("version_name") or "",
            "minecraft_version": resolved["minecraft_version"],
            "modpack_loader": resolved.get("loader") or "",
            "loader_version": resolved.get("loader_version") or "",
            "java_version": java_version,
            "image": image_for(java_version),
        },
        "minecraft_changed": mc_changed,
        "loader_changed": loader_changed,
        "same_release": resolved["version_id"] == instance.modpack_version_id,
        "warnings": warnings,
    }


def _apply_pin(instance: Instance, pin: dict[str, Any]) -> None:
    for field in PIN_FIELDS:
        if field in pin:
            setattr(instance, field, pin[field])
    meta = dict(instance.modpack_meta or {})
    meta["pinned_at_job"] = True
    instance.modpack_meta = meta
    instance.save(update_fields=[*PIN_FIELDS, "modpack_meta"])
    from apps.instances.services import bump_revision

    bump_revision(instance)


@register("modpack_update")
def modpack_update(job: Job, ctx: JobContext) -> dict[str, Any]:
    instance = Instance.objects.select_related("node").get(pk=job.instance_id)
    plan = plan_update(instance, job.params["version_id"])
    if plan["same_release"]:
        raise ApiError("The server already runs this release", code="same_release", status_code=409)
    if plan["minecraft_changed"] and not job.params.get("allow_minecraft_change"):
        raise ApiError(
            "This release uses a different Minecraft version; confirm the world upgrade to continue",
            code="minecraft_change_unconfirmed",
            status_code=409,
        )
    was_running = False
    pinned = False  # the new release is pinned in the database
    backup = None
    try:
        was_running = ensure_stopped(instance, ctx)
        if job.params.get("backup", SystemSetting.get("pre_update_backup")):
            ctx.progress(0.05, "Creating a pre-update backup")
            backup = run_backup(
                instance,
                ctx,
                name=f"Before update to {plan['target']['modpack_version_name'] or plan['target']['modpack_version_id']}",
                kind=BackupKind.PRE_UPDATE,
                user=job.user,
                span=(0.05, 0.5),
                job=job,
            )
            backup.meta = {**(backup.meta or {}), "modpack_before": plan["current"], "modpack_after": plan["target"]}
            backup.save(update_fields=["meta"])
        ctx.check_cancelled()
        ctx.progress(0.55, f"Pinning {instance.modpack_name} {plan['target']['modpack_version_name']}", force=True)
        history = list((instance.modpack_meta or {}).get("history", []))
        history.append(
            {"from": plan["current"], "to": plan["target"], "job": str(job.id), "backup": str(backup.id) if backup else None}
        )
        instance.modpack_meta = {**(instance.modpack_meta or {}), "history": history[-20:]}
        instance.save(update_fields=["modpack_meta"])
        pinned = True
        _apply_pin(instance, plan["target"])
        instance.refresh_from_db()
        ctx.progress(0.6, "Recreating the container; the new release installs on start")
        if was_running:  # (started again without the memory check: it ran when the job began)
            starting_again(ctx, lambda: apply_container(instance, ctx, start=True, span=(0.6, 0.95), admit=False))
        else:
            apply_container(instance, ctx, start=bool(job.params.get("start")), span=(0.6, 0.95))
    except Exception as exc:
        untouched = not pinned or _apply_refused(exc, instance)
        if pinned and untouched:
            # the agent refused the new container or could not get its image: nothing changed
            _apply_pin(instance, plan["current"])
            meta = dict(instance.modpack_meta or {})
            meta["history"] = [h for h in meta.get("history", []) if h.get("job") != str(job.id)]
            instance.modpack_meta = meta
            instance.save(update_fields=["modpack_meta"])
            ctx.log("The update was not applied: the previous release is pinned again")
        put_back(job, instance, ctx, exc, was_running=was_running, untouched=untouched)
        raise
    return {
        "from": plan["current"]["modpack_version_id"],
        "to": plan["target"]["modpack_version_id"],
        "backup_id": str(backup.id) if backup else None,
        "started": was_running or bool(job.params.get("start")),
    }


@register("modpack_rollback")
def modpack_rollback(job: Job, ctx: JobContext) -> dict[str, Any]:
    instance = Instance.objects.select_related("node").get(pk=job.instance_id)
    backup = Backup.objects.get(pk=job.params["backup_id"], instance=instance)
    before = (backup.meta or {}).get("modpack_before")
    if backup.kind != BackupKind.PRE_UPDATE or not before or backup.status != BackupStatus.SUCCEEDED:
        raise ApiError("Only successful pre-update backups can be used to roll back", code="not_rollback_point")
    require_local_backup(backup, instance)
    client = AgentClient(instance.node)
    was_running = False
    progress = Progress(RESTORE_UNTOUCHED)
    try:
        was_running = ensure_stopped(instance, ctx)
        ctx.progress(0.05, "Creating a safety backup of the current state")
        run_backup(
            instance, ctx, name="Before modpack rollback", kind=BackupKind.PRE_RESTORE, user=job.user, span=(0.05, 0.4), job=job
        )
        ctx.check_cancelled()
        ctx.progress(0.45, f"Restoring '{backup.name}'", force=True)
        progress.requested()
        op = client.restore_backup(instance.id, backup.snapshot_id)
        progress.running()
        client.wait_operation(op["id"], timeout=6 * 3600, on_progress=ctx.agent_progress(0.45, 0.35))
        progress.changed()
        _apply_pin(instance, before)
        instance.refresh_from_db()
        if was_running:
            starting_again(ctx, lambda: apply_container(instance, ctx, start=True, span=(0.85, 0.98), admit=False))
        else:
            apply_container(instance, ctx, start=False, span=(0.85, 0.98))
    except Exception as exc:
        # after the restore the files are the old release's: never start the new release's container on them
        put_back(job, instance, ctx, exc, was_running=was_running, untouched=progress.untouched(exc))
        raise
    return {"restored_backup": str(backup.id), "pinned": before.get("modpack_version_id")}
