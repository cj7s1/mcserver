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
from apps.nodes.models import Node

from .models import Backup, BackupKind, BackupStatus, RetentionPolicy
from .retention import select_to_delete

log = logging.getLogger(__name__)

DEFAULT_EXCLUDES = ["logs/*.log.gz", "crash-reports", ".cache", "cache", "debug", "*.tmp", "modernfix/classCache*"]


# --- which node holds a backup -----------------------------------------------------------
# Every node keeps its own restic repository per server, and a migration does not copy it.
# ``Backup.meta["node_id"]`` records the node whose repository holds the snapshot: set when the
# backup is made, and added to older rows when the server is migrated (rows from versions
# that only stored ``left_on_node`` by name are resolved by that name). Untagged rows live on
# the server's current node.
def repository_node_id(backup: Backup, instance: Instance) -> int | None:
    """Id of the node whose restic repository holds ``backup`` (None: unknown/deleted node)."""
    meta = backup.meta or {}
    if meta.get("node_id") is not None:
        return int(meta["node_id"])
    if meta.get("left_on_node"):
        return Node.objects.filter(name=meta["left_on_node"]).values_list("pk", flat=True).first()
    return instance.node_id


def stored_on_current_node(backup: Backup, instance: Instance) -> bool:
    return repository_node_id(backup, instance) == instance.node_id


def repository_node(backup: Backup, instance: Instance) -> Node | None:
    node_id = repository_node_id(backup, instance)
    if node_id is None:
        return None
    if node_id == instance.node_id:
        return instance.node
    return Node.objects.filter(pk=node_id).first()


def _node_label(backup: Backup, instance: Instance) -> str:
    node = repository_node(backup, instance)
    if node is not None:
        return f"node '{node.name}'"
    meta = backup.meta or {}
    name = meta.get("node_name") or meta.get("left_on_node")
    return f"a node that no longer exists ('{name}')" if name else "a node that no longer exists"


def require_local_backup(backup: Backup, instance: Instance) -> None:
    """Refuse in-place restores of a backup that lives in another node's repository."""
    if stored_on_current_node(backup, instance):
        return
    raise ApiError(
        f"This backup is stored in the backup repository on {_node_label(backup, instance)}, but the server now "
        f"runs on '{instance.node.name}', so it cannot be restored in place. Restore it as a new server (created on "
        "the node that holds it) or move the server back to that node first.",
        code="backup_on_other_node",
        status_code=409,
    )


def backup_agent(backup: Backup, instance: Instance) -> AgentClient:
    """Agent of the node whose repository holds ``backup`` (browse, download, delete, restore as new)."""
    node = repository_node(backup, instance)
    if node is None:
        raise ApiError(
            f"This backup is stored on {_node_label(backup, instance)}; it can no longer be read.",
            code="backup_node_missing",
            status_code=409,
        )
    return AgentClient(node)


def local_backups(instance: Instance) -> list[Backup]:
    """Succeeded backups in the repository on the server's current node."""
    return [
        b for b in Backup.objects.filter(instance=instance, status=BackupStatus.SUCCEEDED) if stored_on_current_node(b, instance)
    ]


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
    excludes: list[str] | None = None,
) -> Backup:
    client = AgentClient(instance.node)
    policy = policy_for(instance)
    backup = Backup.objects.create(
        instance=instance,
        name=name[:120],
        kind=kind,
        created_by=user,
        job=job,
        meta={"node_id": instance.node_id, "node_name": instance.node.name},
    )
    try:
        op = client.create_backup(
            instance.id,
            str(backup.id),
            tags=[f"kind:{kind}"],
            consistency=consistency,
            excludes=DEFAULT_EXCLUDES + list(policy.excludes or []) + list(excludes or []),
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
    # only this node's repository: backups left on a previous node can neither be pruned from
    # here nor count towards what is kept here
    candidates = local_backups(instance)
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
    require_local_backup(backup, instance)
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
    client = backup_agent(backup, source)
    if target.node_id != client.node.pk:
        # the snapshot is in another node's repository (the source server was migrated since):
        # restic restores on the node that holds it, so the new server is created there
        from apps.instances.services import allocate_port, ports_in_use

        if target.game_port in ports_in_use(client.node, exclude=target):
            target.game_port = allocate_port(client.node, exclude=target)
        target.node = client.node
        target.save(update_fields=["node", "game_port"])
        ctx.log(f"The backup is stored on {client.node.name}; the restored server is created there")
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
        if repository_node(backup, instance) is None:
            ctx.log(f"The backup was stored on {_node_label(backup, instance)}; removing the record only")
        else:
            client = backup_agent(backup, instance)
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
    # the check covered this node's repository only
    Backup.objects.filter(pk__in=[b.pk for b in local_backups(instance)]).update(verified_at=timezone.now(), verify_ok=ok)
    if not ok:
        raise ApiError("Integrity check FAILED: " + " | ".join(result.get("output", [])[-5:]), code="verify_failed")
    return {"ok": ok, "output": result.get("output", [])}


@register("restore_path")
def restore_path_job(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Restore a single file or folder from a backup (server stopped first, restarted after)."""
    instance = Instance.objects.select_related("node").get(pk=job.instance_id)
    backup = Backup.objects.get(pk=job.params["backup_id"], instance=instance)
    require_local_backup(backup, instance)
    client = AgentClient(instance.node)
    was_running = ensure_stopped(instance, ctx)
    ctx.progress(0.2, f"Restoring {job.params['path']} from '{backup.name}'", force=True)
    op = client.backup_restore_path(instance.id, backup.snapshot_id, job.params["path"])
    client.wait_operation(op["id"], timeout=6 * 3600, on_progress=ctx.agent_progress(0.2, 0.7))
    if was_running:
        ctx.progress(0.92, "Starting server")
        client.start(instance.id)
    return {"restored": job.params["path"], "backup": str(backup.id), "restarted": was_running}


# Snapshot jobs are provider-agnostic: the node's agent picks btrfs or ZFS by filesystem.
CONVERT_MESSAGES = {
    "btrfs": "Converting the server folder to a btrfs subvolume (reflink copy)",
    "zfs": "Converting the server folder to its own ZFS dataset (copying the files)",
}


@register("snapshot_create")
def snapshot_create(job: Job, ctx: JobContext) -> dict[str, Any]:
    instance = Instance.objects.select_related("node").get(pk=job.instance_id)
    client = AgentClient(instance.node)
    ctx.progress(0.1, "Creating filesystem snapshot", force=True)
    op = client.create_snapshot(
        instance.id, job.params.get("label", ""), job.params.get("consistency", "auto"), job.params.get("kind", "manual")
    )
    done = client.wait_operation(op["id"], timeout=1800, on_progress=ctx.agent_progress(0.1, 0.8))
    result = done.get("result") or {}
    keep = job.params.get("keep")
    if keep and job.params.get("kind") == "scheduled":
        removed = prune_scheduled_snapshots(
            client,
            instance,
            ctx,
            label=job.params.get("label", ""),
            keep=int(keep),
            schedule_id=job.params.get("schedule_id"),
            legacy_label=job.params.get("legacy_label", ""),
        )
        if removed:
            result = {**result, "retention_removed": removed}
    return result


def schedule_snapshot_label(schedule_id: int, name: str) -> str:
    """Label of the snapshots a schedule takes: unique per schedule, even when names repeat."""
    return f"scheduled #{schedule_id} {name}"[:120]


def _of_schedule(snap_label: str, *, label: str, schedule_id: Any, legacy_label: str) -> bool:
    if schedule_id is not None:
        prefix = f"scheduled #{int(schedule_id)}"
        # by id, so renaming the schedule keeps its older snapshots under its retention
        if snap_label == prefix or snap_label.startswith(prefix + " "):
            return True
    return snap_label == label or bool(legacy_label and snap_label == legacy_label)


def prune_scheduled_snapshots(
    client: AgentClient,
    instance: Instance,
    ctx: JobContext,
    *,
    label: str,
    keep: int,
    schedule_id: Any = None,
    legacy_label: str = "",
) -> list[str]:
    """Keep the newest ``keep`` snapshots of ONE snapshot schedule and delete its older ones.

    A schedule's snapshots are those of kind "scheduled" whose label carries its id (the
    scheduler labels them "scheduled #<id> <schedule name>", ``schedule_snapshot_label``), or
    exactly ``label`` / ``legacy_label`` (the "scheduled <name>" labels of older versions, passed
    only while that name is unique among the server's snapshot schedules). Each schedule's
    ``keep`` therefore applies to its own snapshots only, even when two schedules share a name.
    Manual and safety (pre-restore/pre-update) snapshots, snapshots of other schedules and
    unlabelled ones are never touched. Deletion is best effort.
    """
    if keep < 1 or not label:
        return []
    listed = client.snapshots(instance.id)["snapshots"]
    mine = [
        s
        for s in listed
        if s.get("kind") == "scheduled"
        and s.get("name")
        and _of_schedule(str(s.get("label") or ""), label=label, schedule_id=schedule_id, legacy_label=legacy_label)
    ]
    mine.sort(key=lambda s: s["name"], reverse=True)  # names start with a UTC timestamp: newest first
    removed = []
    for old in mine[keep:]:
        try:
            client.delete_snapshot(instance.id, old["name"])
        except ApiError as exc:
            if exc.error_code == "snapshot_has_dependents":
                # ZFS: snapshots kept from before a rollback are based on it; it can go once those do.
                ctx.log(f"Retention: kept snapshot {old['name']}: {exc.detail}")
            else:
                ctx.log(f"Retention: could not delete snapshot {old['name']}: {exc.detail}")
            continue
        removed.append(old["name"])
        ctx.log(f"Retention: deleted snapshot {old['name']}")
    return removed


@register("snapshot_restore")
def snapshot_restore(job: Job, ctx: JobContext) -> dict[str, Any]:
    instance = Instance.objects.select_related("node").get(pk=job.instance_id)
    client = AgentClient(instance.node)
    was_running = ensure_stopped(instance, ctx)
    name = job.params["name"]
    ctx.progress(0.3, "Taking a safety snapshot of the current state", force=True)
    op = client.create_snapshot(instance.id, f"before rollback to {name}"[:120], "none", "pre_restore")
    client.wait_operation(op["id"], timeout=1800)
    ctx.progress(0.6, f"Rolling back to snapshot {name}", force=True)
    op = client.restore_snapshot(instance.id, name)
    done = client.wait_operation(op["id"], timeout=3600)
    # ZFS rollbacks report tidy-up problems (e.g. a history dataset that could not be promoted).
    warnings = [str(w) for w in (done.get("result") or {}).get("warnings") or []]
    for warning in warnings:
        ctx.log(f"Warning: {warning}")
    if was_running:
        ctx.progress(0.9, "Starting server")
        client.start(instance.id)
    return {"restored": name, "restarted": was_running, "warnings": warnings}


@register("snapshot_delete")
def snapshot_delete(job: Job, ctx: JobContext) -> dict[str, Any]:
    instance = Instance.objects.select_related("node").get(pk=job.instance_id)
    AgentClient(instance.node).delete_snapshot(instance.id, job.params["name"])
    return {"deleted": job.params["name"]}


@register("snapshot_convert")
def snapshot_convert(job: Job, ctx: JobContext) -> dict[str, Any]:
    instance = Instance.objects.select_related("node").get(pk=job.instance_id)
    client = AgentClient(instance.node)
    provider = (client.snapshots(instance.id).get("capability") or {}).get("provider")
    was_running = ensure_stopped(instance, ctx)
    ctx.progress(0.2, CONVERT_MESSAGES.get(provider, "Preparing the server folder for snapshots"), force=True)
    op = client.convert_snapshots(instance.id)
    done = client.wait_operation(op["id"], timeout=6 * 3600, on_progress=ctx.agent_progress(0.2, 0.7))
    if was_running:
        client.start(instance.id)
    return done.get("result") or {}
