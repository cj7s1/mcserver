"""Backup / restore job handlers."""

from __future__ import annotations

import logging
from typing import Any

from django.utils import timezone

from apps.core.errors import ApiError
from apps.core.models import SystemSetting
from apps.instances.handlers import apply_container, ensure_stopped
from apps.instances.models import Instance
from apps.jobs.models import Job
from apps.jobs.runner import JobContext, register
from apps.nodes.client import AgentClient

from .models import Backup, BackupKind, BackupStatus, RetentionPolicy
from .retention import select_to_delete

log = logging.getLogger(__name__)

DEFAULT_EXCLUDES = ["logs/*.log.gz", "crash-reports", ".cache", "cache", "debug", "*.tmp", "modernfix/classCache*"]


def policy_for(instance: Instance) -> RetentionPolicy:
    policy = RetentionPolicy.objects.filter(instance=instance).first()
    if policy is None:
        d = SystemSetting.get("backup_default_retention") or {}
        policy = RetentionPolicy.objects.create(
            instance=instance,
            keep_last=d.get("keep_last", 10),
            keep_daily=d.get("keep_daily", 7),
            keep_weekly=d.get("keep_weekly", 4),
            keep_monthly=d.get("keep_monthly", 0),
        )
    return policy


def run_backup(
    instance: Instance,
    ctx: JobContext,
    *,
    name: str,
    kind: str,
    user: Any = None,
    span: tuple[float, float] = (0.0, 1.0),
    consistency: str = "auto",
    job: Job | None = None,
) -> Backup:
    client = AgentClient(instance.node)
    policy = policy_for(instance)
    backup = Backup.objects.create(instance=instance, name=name[:120], kind=kind, created_by=user, job=job)
    try:
        op = client.create_backup(
            instance.id,
            str(backup.id),
            tags=[f"kind:{kind}"],
            consistency=consistency,
            excludes=DEFAULT_EXCLUDES + list(policy.excludes or []),
        )
        done = client.wait_operation(
            op["id"], timeout=6 * 3600, interval=1.5, on_progress=ctx.agent_progress(span[0], span[1] - span[0])
        )
    except ApiError as exc:
        backup.status = BackupStatus.FAILED
        backup.error = str(exc.detail)[:4000]
        backup.finished_at = timezone.now()
        backup.save(update_fields=["status", "error", "finished_at"])
        raise
    result = done.get("result") or {}
    backup.status = BackupStatus.SUCCEEDED
    backup.snapshot_id = result.get("snapshot_id", "")
    backup.data_added_bytes = result.get("data_added")
    backup.total_bytes = result.get("total_bytes_processed")
    backup.files_count = (
        (result.get("files_new") or 0) + (result.get("files_changed") or 0) + (result.get("files_unmodified") or 0)
    )
    backup.duration_seconds = result.get("total_duration")
    backup.consistent = bool(result.get("consistent"))
    backup.warnings = result.get("warnings") or []
    backup.finished_at = timezone.now()
    backup.save()
    for w in backup.warnings:
        ctx.log(f"warning: {w}")
    return backup


def apply_retention(instance: Instance, ctx: JobContext) -> list[str]:
    policy = policy_for(instance)
    candidates = list(Backup.objects.filter(instance=instance, status=BackupStatus.SUCCEEDED))
    doomed = select_to_delete(
        candidates,
        keep_last=policy.keep_last,
        keep_daily=policy.keep_daily,
        keep_weekly=policy.keep_weekly,
        keep_monthly=policy.keep_monthly,
        keep_safety_days=policy.keep_safety_days,
        now=timezone.now(),
    )
    ids = [b.snapshot_id for b in doomed if b.snapshot_id]
    if not ids:
        return []
    ctx.progress(None, f"Retention: removing {len(ids)} old backup(s)")
    client = AgentClient(instance.node)
    op = client.forget_backups(instance.id, ids, prune=True)
    client.wait_operation(op["id"], timeout=3 * 3600)
    Backup.objects.filter(pk__in=[b.pk for b in doomed]).update(status=BackupStatus.DELETED)
    return ids


@register("backup")
def backup_job(job: Job, ctx: JobContext) -> dict[str, Any]:
    instance = Instance.objects.select_related("node").get(pk=job.instance_id)
    kind = job.params.get("kind", BackupKind.MANUAL)
    name = job.params.get("name") or f"Backup {timezone.localtime().strftime('%Y-%m-%d %H:%M')}"
    backup = run_backup(
        instance,
        ctx,
        name=name,
        kind=kind,
        user=job.user,
        span=(0.0, 0.9),
        consistency=job.params.get("consistency", "auto"),
        job=job,
    )
    removed: list[str] = []
    if kind in (BackupKind.MANUAL, BackupKind.SCHEDULED):
        removed = apply_retention(instance, ctx)
    return {
        "backup_id": str(backup.id),
        "snapshot_id": backup.snapshot_id,
        "consistent": backup.consistent,
        "retention_removed": removed,
    }


@register("restore")
def restore_job(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Restore a backup over the existing instance, after a safety backup."""
    instance = Instance.objects.select_related("node").get(pk=job.instance_id)
    backup = Backup.objects.get(pk=job.params["backup_id"], instance=instance)
    client = AgentClient(instance.node)
    was_running = ensure_stopped(instance, ctx)
    safety = None
    if job.params.get("pre_restore_backup", True):
        ctx.progress(0.05, "Creating a safety backup of the current state")
        safety = run_backup(
            instance,
            ctx,
            name=f"Before restoring '{backup.name}'",
            kind=BackupKind.PRE_RESTORE,
            user=job.user,
            span=(0.05, 0.45),
            job=job,
        )
    ctx.progress(0.5, f"Restoring '{backup.name}'", force=True)
    op = client.restore_backup(instance.id, backup.snapshot_id)
    client.wait_operation(op["id"], timeout=6 * 3600, on_progress=ctx.agent_progress(0.5, 0.4))
    if was_running:
        ctx.progress(0.92, "Starting server")
        client.start(instance.id)
    return {"restored": str(backup.id), "safety_backup": str(safety.id) if safety else None, "restarted": was_running}


@register("restore_new")
def restore_new_job(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Restore a backup into a newly created instance (job runs under the source instance lock)."""
    source = Instance.objects.select_related("node").get(pk=job.instance_id)
    target = Instance.objects.select_related("node").get(pk=job.params["target_id"])
    backup = Backup.objects.get(pk=job.params["backup_id"], instance=source)
    client = AgentClient(source.node)
    ctx.progress(0.05, f"Restoring '{backup.name}' into {target.name}", force=True)
    op = client.restore_backup(source.id, backup.snapshot_id, target_instance_id=target.id)
    client.wait_operation(op["id"], timeout=6 * 3600, on_progress=ctx.agent_progress(0.05, 0.8))
    target.installed_at = timezone.now()
    target.save(update_fields=["installed_at"])
    apply_container(target, ctx, start=False, span=(0.88, 0.98))
    return {"target_id": str(target.id)}


@register("backup_delete")
def delete_job(job: Job, ctx: JobContext) -> dict[str, Any]:
    instance = Instance.objects.select_related("node").get(pk=job.instance_id)
    backup = Backup.objects.get(pk=job.params["backup_id"], instance=instance)
    if backup.snapshot_id:
        client = AgentClient(instance.node)
        op = client.forget_backups(instance.id, [backup.snapshot_id], prune=True)
        client.wait_operation(op["id"], timeout=3 * 3600, on_progress=ctx.agent_progress(0.1, 0.8))
    backup.status = BackupStatus.DELETED
    backup.save(update_fields=["status"])
    return {"deleted": str(backup.id)}


@register("backup_verify")
def verify_job(job: Job, ctx: JobContext) -> dict[str, Any]:
    instance = Instance.objects.select_related("node").get(pk=job.instance_id)
    client = AgentClient(instance.node)
    subset = job.params.get("read_data_subset") or "10%"
    ctx.progress(0.1, f"Checking repository integrity (reading {subset} of data)", force=True)
    op = client.check_backups(instance.id, subset)
    done = client.wait_operation(op["id"], timeout=6 * 3600, on_progress=ctx.agent_progress(0.1, 0.85))
    result = done.get("result") or {}
    ok = bool(result.get("ok"))
    Backup.objects.filter(instance=instance, status=BackupStatus.SUCCEEDED).update(verified_at=timezone.now(), verify_ok=ok)
    if not ok:
        raise ApiError("Integrity check FAILED: " + " | ".join(result.get("output", [])[-5:]), code="verify_failed")
    return {"ok": ok, "output": result.get("output", [])}
