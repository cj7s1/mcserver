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
    client.wait_operation(op["id"], timeout=3600, on_progress=ctx.agent_progress(0.1, 0.8))
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
