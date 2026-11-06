"""Job handlers for the instance lifecycle (registered with the job runner)."""

from __future__ import annotations

from typing import Any

from django.utils import timezone

from apps.core.errors import ApiError, ConflictError
from apps.jobs.models import Job
from apps.jobs.runner import JobContext, register
from apps.nodes.client import AgentClient

from . import properties
from .models import DesiredState, Instance, Status
from .reconcile import reconcile_instance
from .spec import build_spec, restart_required, spec_hash


def _instance(job: Job) -> Instance:
    inst = Instance.objects.select_related("node").get(pk=job.instance_id)
    return inst


def apply_container(
    instance: Instance, ctx: JobContext, *, start: bool, span: tuple[float, float] = (0.1, 0.8)
) -> dict[str, Any]:
    """(Re)create the container from the current configuration. World data is untouched."""
    client = AgentClient(instance.node)
    spec = build_spec(instance)
    ctx.progress(span[0], "Preparing container (pulling the runtime image if needed)", force=True)
    op = client.apply(instance.id, spec, start=start)
    result = client.wait_operation(op["id"], timeout=3600, on_progress=ctx.agent_progress(span[0], span[1]))
    instance.container_spec_hash = spec_hash(spec)
    instance.applied_revision = Instance.objects.filter(pk=instance.pk).values_list("config_revision", flat=True).first() or 0
    instance.save(update_fields=["container_spec_hash", "applied_revision"])
    return result.get("result", {})


def _set_desired(instance: Instance, desired: str) -> None:
    instance.desired_state = desired
    Instance.objects.filter(pk=instance.pk).update(desired_state=desired)


@register("install")
def install(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Create the data directory, write initial config, create the container and optionally start it."""
    instance = _instance(job)
    client = AgentClient(instance.node)
    initial = job.params.get("server_properties") or {}
    files: dict[str, str] = {}
    if initial:
        files["server.properties"] = properties.render(initial)
    ctx.progress(0.02, "Creating instance directory", force=True)
    client.prepare(instance.id, files)
    start = bool(job.params.get("start", True))
    if start:
        _set_desired(instance, DesiredState.RUNNING)
    apply_container(instance, ctx, start=start, span=(0.05, 0.9))
    if start:
        ctx.progress(0.95, "Server container started; the first start downloads and installs the server software")
    return {"started": start}


@register("start")
def start(job: Job, ctx: JobContext) -> dict[str, Any]:
    instance = _instance(job)
    client = AgentClient(instance.node)
    _set_desired(instance, DesiredState.RUNNING)
    status = client.status(instance.id)
    if not status.get("exists") or restart_required(instance) or not instance.container_spec_hash:
        ctx.progress(0.1, "Applying configuration")
        apply_container(instance, ctx, start=True, span=(0.1, 0.8))
    else:
        ctx.progress(0.3, "Starting container")
        client.start(instance.id)
    return {}


@register("stop")
def stop(job: Job, ctx: JobContext) -> dict[str, Any]:
    instance = _instance(job)
    _set_desired(instance, DesiredState.STOPPED)
    ctx.progress(0.2, f"Stopping server gracefully (up to {instance.stop_timeout}s)", force=True)
    status = AgentClient(instance.node).status(instance.id)
    if status.get("exists"):
        AgentClient(instance.node).stop(instance.id, timeout=instance.stop_timeout)
    return {}


@register("restart")
def restart(job: Job, ctx: JobContext) -> dict[str, Any]:
    instance = _instance(job)
    client = AgentClient(instance.node)
    _set_desired(instance, DesiredState.RUNNING)
    status = client.status(instance.id)
    if not status.get("exists") or restart_required(instance):
        ctx.progress(0.1, "Configuration changed; recreating container (world data is kept)")
        if status.get("running"):
            client.stop(instance.id, timeout=instance.stop_timeout)
        apply_container(instance, ctx, start=True, span=(0.2, 0.9))
    else:
        ctx.progress(0.2, "Restarting server", force=True)
        client.restart(instance.id, timeout=instance.stop_timeout)
    return {}


@register("recreate")
def recreate(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Recreate the container from the current configuration without touching world data."""
    instance = _instance(job)
    client = AgentClient(instance.node)
    status = client.status(instance.id)
    was_running = bool(status.get("running"))
    if was_running:
        ctx.progress(0.05, "Stopping server", force=True)
        client.stop(instance.id, timeout=instance.stop_timeout)
    apply_container(instance, ctx, start=was_running or job.params.get("start", False), span=(0.1, 0.95))
    return {"restarted": was_running}


@register("archive")
def archive(job: Job, ctx: JobContext) -> dict[str, Any]:
    instance = _instance(job)
    client = AgentClient(instance.node)
    ctx.progress(0.1, "Stopping server", force=True)
    _set_desired(instance, DesiredState.STOPPED)
    client.remove_container(instance.id, timeout=instance.stop_timeout)
    instance.archived_at = timezone.now()
    instance.container_spec_hash = ""
    instance.status = Status.ARCHIVED
    instance.save(update_fields=["archived_at", "container_spec_hash", "status"])
    return {}


@register("delete")
def delete(job: Job, ctx: JobContext) -> dict[str, Any]:
    instance = _instance(job)
    client = AgentClient(instance.node)
    _set_desired(instance, DesiredState.STOPPED)
    ctx.progress(0.1, "Deleting container, files and backups", force=True)
    op = client.delete_data(instance.id)
    done = client.wait_operation(op["id"], timeout=3600, on_progress=ctx.agent_progress(0.1, 0.8))
    for warning in (done.get("result") or {}).get("warnings") or []:
        ctx.log(f"Warning: {warning}")  # e.g. a btrfs snapshot that could not be deleted
    name = instance.name
    instance_id = str(instance.id)
    # detach the job from the instance before deleting so the job history survives
    Job.objects.filter(instance=instance).update(instance=None)
    job.instance_id = None
    instance.delete()
    return {"deleted": instance_id, "name": name}


@register("clone")
def clone(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Copy the source instance's files into the (already created) target instance."""
    source = _instance(job)
    target = Instance.objects.select_related("node").get(pk=job.params["target_id"])
    client = AgentClient(source.node)
    status = client.status(source.id)
    if status.get("running"):
        raise ConflictError("Stop the source server before cloning it", code="source_running")
    ctx.progress(0.05, "Copying server files", force=True)
    op = client.clone(source.id, target.id, exclude_worlds=bool(job.params.get("exclude_worlds")))
    client.wait_operation(op["id"], timeout=7200, on_progress=ctx.agent_progress(0.05, 0.75))
    target.installed_at = source.installed_at
    target.save(update_fields=["installed_at"])
    ctx.progress(0.85, "Creating container for the clone")
    apply_container(target, ctx, start=False, span=(0.85, 0.98))
    return {"target_id": str(target.id)}


def ensure_stopped(instance: Instance, ctx: JobContext) -> bool:
    """Stop the server if running. Returns True if it was running."""
    client = AgentClient(instance.node)
    status = client.status(instance.id)
    if status.get("running"):
        ctx.progress(None, "Stopping server", force=True)
        client.stop(instance.id, timeout=instance.stop_timeout)
        reconcile_instance(instance.id)
        return True
    return False


def require_not_running(instance: Instance) -> None:
    status = AgentClient(instance.node).status(instance.id)
    if status.get("running"):
        raise ApiError("The server must be stopped for this operation", code="must_stop", status_code=409)


@register("extract_archive")
def extract_archive(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Safely extract an uploaded archive inside the instance (import server / world / server pack)."""
    instance = _instance(job)
    client = AgentClient(instance.node)
    require_not_running(instance)
    p = job.params
    ctx.progress(0.02, f"Extracting {p['path']}", force=True)
    op = client.extract(
        instance.id,
        p["path"],
        p.get("destination", ""),
        strip=p.get("strip", "auto"),
        overwrite=bool(p.get("overwrite")),
        delete_archive=bool(p.get("delete_archive")),
    )
    done = client.wait_operation(op["id"], timeout=6 * 3600, on_progress=ctx.agent_progress(0.02, 0.95))
    result = done.get("result") or {}
    if result.get("existing_count"):
        ctx.log(f"{result['existing_count']} existing file(s) were kept (overwrite was off)")
    if result.get("skipped_count"):
        ctx.log(f"{result['skipped_count']} unsafe entries skipped (links, special files or unsafe paths)")
    if p.get("mark_installed") and instance.installed_at is None:
        from django.utils import timezone

        instance.installed_at = timezone.now()
        instance.save(update_fields=["installed_at"])
    return result


@register("software_update")
def software_update(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Change Minecraft/loader/Java versions of a standard server, protected by a pre-update backup."""
    from apps.backups.handlers import run_backup
    from apps.backups.models import BackupKind

    from .spec import image_for

    instance = _instance(job)
    p = job.params
    before = {
        "minecraft_version": instance.minecraft_version,
        "loader_version": instance.loader_version,
        "java_version": instance.java_version,
        "image": instance.image,
    }
    was_running = ensure_stopped(instance, ctx)
    backup_id = None
    if p.get("backup", True):
        ctx.progress(0.05, "Creating a pre-update backup")
        backup = run_backup(
            instance,
            ctx,
            name=f"Before update to {p['minecraft_version']}",
            kind=BackupKind.PRE_UPDATE,
            user=job.user,
            span=(0.05, 0.55),
            job=job,
        )
        backup.meta = {"software_before": before}
        backup.save(update_fields=["meta"])
        backup_id = str(backup.id)
    instance.minecraft_version = p["minecraft_version"]
    instance.loader_version = p.get("loader_version", "")
    instance.java_version = int(p["java_version"])
    instance.image = image_for(instance.java_version)
    instance.save(update_fields=["minecraft_version", "loader_version", "java_version", "image"])
    from .services import bump_revision

    bump_revision(instance)
    instance.refresh_from_db()
    ctx.progress(0.6, f"Recreating container for {instance.server_type} {instance.minecraft_version}", force=True)
    apply_container(instance, ctx, start=was_running or bool(p.get("start")), span=(0.6, 0.97))
    return {"before": before, "after": {k: getattr(instance, k) for k in before}, "backup_id": backup_id}


@register("migrate")
def migrate(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Move a server to another node: stream its folder agent-to-agent, then recreate it there.

    restic backups stay in the source node's repository (they are not copied), so a fresh
    backup is taken on the target afterwards when requested.
    """
    import secrets as _secrets

    from apps.nodes.models import Node

    from .services import allocate_port, ports_in_use

    instance = _instance(job)
    source = AgentClient(instance.node)
    target_node = Node.objects.get(pk=job.params["target_node"])
    target = AgentClient(target_node)
    if target_node.pk == instance.node_id:
        raise ApiError("The server is already on that node", code="same_node", status_code=409)
    target.info()  # fail fast if the target agent is unreachable or rejects the token
    was_running = ensure_stopped(instance, ctx)

    ctx.progress(0.05, f"Preparing {target_node.name}", force=True)
    target.prepare(instance.id)
    archive = f".mcpanel-migrate-{_secrets.token_hex(6)}.zip"
    ctx.progress(0.1, "Copying server files to the target node (streamed)", force=True)
    chunks, _length, _resp = source.download(instance.id, "", endpoint="archive")
    uploaded = target.upload(instance.id, archive, chunks, overwrite=False)
    ctx.log(f"Transferred {uploaded.get('size', 0)} bytes")
    ctx.progress(0.6, "Unpacking on the target node")
    op = target.extract(instance.id, archive, "", strip="auto", overwrite=True, delete_archive=True)
    target.wait_operation(op["id"], timeout=6 * 3600, on_progress=ctx.agent_progress(0.6, 0.25))

    old_node = instance.node
    port_changed = None
    if instance.game_port in ports_in_use(target_node, exclude=instance):
        new_port = allocate_port(target_node, exclude=instance)
        port_changed = (instance.game_port, new_port)
        instance.game_port = new_port
    instance.node = target_node
    instance.container_spec_hash = ""
    instance.save(update_fields=["node", "game_port", "container_spec_hash"])
    ctx.progress(0.88, f"Creating the container on {target_node.name}")
    apply_container(instance, ctx, start=was_running, span=(0.88, 0.97))

    source.remove_container(instance.id, timeout=30)
    if job.params.get("delete_source"):
        ctx.log(f"Deleting the server folder and backups on {old_node.name}")
        op = source.delete_data(instance.id)
        source.wait_operation(op["id"], timeout=3600)
    from apps.backups.models import Backup, BackupStatus

    stale = Backup.objects.filter(instance=instance, status=BackupStatus.SUCCEEDED)
    stale_count = stale.count()
    if stale_count:
        meta_note = {"left_on_node": old_node.name}
        for b in stale:
            b.meta = {**(b.meta or {}), **meta_note}
            b.status = BackupStatus.DELETED if job.params.get("delete_source") else b.status
            b.save(update_fields=["meta", "status"])
    return {
        "from": old_node.name,
        "to": target_node.name,
        "bytes": uploaded.get("size"),
        "port_changed": port_changed,
        "restarted": was_running,
        "source_data_deleted": bool(job.params.get("delete_source")),
        "backups_left_on_source": 0 if job.params.get("delete_source") else stale_count,
    }
