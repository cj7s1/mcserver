"""Backup / restore job handlers."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps import games
from apps.core.errors import ApiError, ConflictError
from apps.core.models import SystemSetting
from apps.instances.handlers import (
    SOFTWARE_FIELDS,
    Progress,
    apply_container,
    describe_software,
    ensure_stopped,
    put_back,
    set_software,
    software_state,
    start_again,
)
from apps.instances.models import Instance
from apps.jobs.models import ACTIVE_STATUSES, Job
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
    policy_excludes: bool = True,
) -> Backup:
    """Take a restic backup of the server folder and record it.

    ``policy_excludes=False`` leaves out the exclusion patterns of the server's backup policy
    (users edit them freely, so they could leave the very data out that a safety backup is
    for); the built-in ``DEFAULT_EXCLUDES`` and ``excludes`` always apply.
    """
    client = AgentClient(instance.node)
    policy = policy_for(instance)
    backup = Backup.objects.create(
        instance=instance,
        name=name[:120],
        kind=kind,
        created_by=user,
        job=job,
        # the software the files belong to: a restore puts it back with them (restore_job)
        meta={"node_id": instance.node_id, "node_name": instance.node.name, "software": software_state(instance)},
    )
    try:
        op = client.create_backup(
            instance.id,
            str(backup.id),
            tags=[f"kind:{kind}"],
            consistency=consistency,
            excludes=DEFAULT_EXCLUDES
            + _game_excludes(instance)
            + (list(policy.excludes or []) if policy_excludes else [])
            + list(excludes or []),
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


# --- backups whose job ended without settling them ---------------------------------------------
# run_backup marks its row failed when the agent reports an error. A job that is cut off (the
# panel container was restarted, the worker was killed, someone marked the job as failed) never
# gets there: its row would read "running" for good, although the job is long over. The agent
# goes on with a backup it has started, so the snapshot may even exist. The job watchdog
# (apps/jobs/tasks.py, every 2 minutes) settles such rows here.
ADOPTED_WARNING = (
    "The job that started this backup did not see it finish (it was interrupted or marked as failed). The node's "
    "agent completed the backup, and the panel took it over afterwards. Whether the world was paused while it was "
    "taken is not known."
)
# How long a row whose agent cannot be asked is left alone before it is marked failed anyway.
SETTLE_GIVE_UP_AFTER = timedelta(hours=1)
# A row without a job is only looked at once it is this old (its job was deleted, not just created).
SETTLE_JOBLESS_AFTER = timedelta(minutes=10)
SETTLE_AGENT_TIMEOUT = 60.0


def _snapshot_of(backup: Backup, snapshots: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The finished snapshot the agent tagged with this backup's id (the newest, if several)."""
    tag = f"backup:{backup.id}"
    found = [s for s in snapshots if tag in (s.get("tags") or []) and s.get("id")]
    return max(found, key=lambda s: str(s.get("time") or "")) if found else None


def _when(value: Any) -> datetime | None:
    """A time as restic writes it (RFC 3339 with nanoseconds), or None."""
    try:
        return parse_datetime(str(value or ""))
    except (ValueError, TypeError):
        return None


def _adopt(backup: Backup, snapshot: dict[str, Any]) -> None:
    summary = snapshot.get("summary") or {}
    counts = [summary.get(key) for key in ("files_new", "files_changed", "files_unmodified")]
    started, ended = _when(summary.get("backup_start")), _when(summary.get("backup_end"))
    backup.status = BackupStatus.SUCCEEDED
    backup.snapshot_id = str(snapshot["id"])
    backup.data_added_bytes = summary.get("data_added")
    backup.total_bytes = summary.get("total_bytes_processed")
    backup.files_count = (
        sum(c or 0 for c in counts) if any(c is not None for c in counts) else summary.get("total_files_processed")
    )
    backup.duration_seconds = (ended - started).total_seconds() if started and ended else None
    backup.consistent = False  # only the agent operation's result says so, and that is gone
    backup.warnings = [*(backup.warnings or []), ADOPTED_WARNING]
    backup.error = ""
    backup.finished_at = ended or _when(snapshot.get("time")) or timezone.now()
    backup.save()


def settle_orphaned_backups(now: datetime | None = None) -> dict[str, int]:
    """Settle backup rows that still read "running" although their job has ended.

    For each one the node that holds the repository is asked: a finished snapshot tagged with
    the backup's id is taken over (the row becomes "succeeded" with the snapshot's data);
    without one the row becomes "failed". While the agent still works on the server (its
    operation goes on after the job was cut off) or cannot be asked, the row is left for the
    next run, for at most SETTLE_GIVE_UP_AFTER after the job ended.
    """
    now = now or timezone.now()
    result = {"adopted": 0, "failed": 0, "waiting": 0}
    rows = Backup.objects.filter(status=BackupStatus.RUNNING).select_related("instance__node", "job")
    for backup in rows:
        job = backup.job
        if job is not None and job.status in ACTIVE_STATUSES:
            continue  # being taken right now
        if job is None and now - backup.created_at < SETTLE_JOBLESS_AFTER:
            continue
        ended_at = (job.finished_at if job is not None else None) or backup.created_at
        why = (
            f"job '{job.title}' ended as {job.status}" + (f": {job.error}" if job.error else "")
            if job is not None
            else "its job is gone"
        )
        give_up = now - ended_at > SETTLE_GIVE_UP_AFTER
        snapshot = None
        busy = False
        note = ""
        node = repository_node(backup, backup.instance)
        if node is None:
            note = " The node that held it no longer exists."
        else:
            try:
                client = AgentClient(node, timeout=SETTLE_AGENT_TIMEOUT)
                snapshot = _snapshot_of(backup, client.list_backups(backup.instance_id, timeout=SETTLE_AGENT_TIMEOUT))
                if snapshot is None:
                    operation = client.status(backup.instance_id).get("operation") or {}
                    busy = operation.get("kind") == "backup" and not operation.get("finished_at")
            except ApiError as exc:
                if not give_up:
                    log.info("backup %s: the agent of node %s cannot be asked yet (%s)", backup.id, node.name, exc)
                    result["waiting"] += 1
                    continue
                note = f" The node's agent could not be asked whether it finished the backup ({exc.detail})."
        if snapshot is not None:
            _adopt(backup, snapshot)
            log.warning("backup %s (%s) was finished by the agent after %s; taken over", backup.id, backup.name, why)
            result["adopted"] += 1
        elif busy and not give_up:
            result["waiting"] += 1  # the agent is still writing it: looked at again by the next run
        else:
            backup.status = BackupStatus.FAILED
            backup.error = f"The backup was not finished: {why}.{note}"[:4000]
            backup.finished_at = now
            backup.save(update_fields=["status", "error", "finished_at"])
            log.warning("backup %s (%s) marked failed: %s", backup.id, backup.name, backup.error)
            result["failed"] += 1
    return result


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
    client = AgentClient(instance.node)
    orphans = given_up_snapshots(instance, client)
    if orphans:
        ctx.log(
            f"Retention: also removing {len(orphans)} snapshot(s) of backups the panel had given up on or deleted "
            "(the agent finished or kept them after their job ended)"
        )
    if not ids and not orphans:
        return []
    ctx.check_cancelled()
    ctx.progress(None, f"Retention: removing {len(ids) + len(orphans)} old backup(s)")
    op = client.forget_backups(instance.id, ids + orphans, prune=True)
    client.wait_operation(op["id"], timeout=3 * 3600)
    Backup.objects.filter(pk__in=[b.pk for b in doomed]).update(status=BackupStatus.DELETED)
    return ids + orphans


def given_up_snapshots(instance: Instance, client: AgentClient) -> list[str]:
    """Snapshots in this node's repository whose backup row is failed or deleted.

    A backup the panel stopped waiting for (the agent was out of reach, the job was cut off and
    ``settle_orphaned_backups`` gave up, or it was marked as failed) can still be finished by the
    agent, and a deleted backup whose forget did not remove its snapshot stays in the repository.
    Neither is ever offered for a restore, so retention removes them. Snapshots without a backup
    row of this server (another panel's, or ones ``manage.py adopt_backups`` can take over) are
    left alone. Best effort: [] when the repository cannot be listed.
    """
    try:
        listed = client.list_backups(instance.id)
    except ApiError as exc:
        log.info(
            "retention: the snapshots of %s cannot be listed (%s); none are pruned besides the old backups", instance.pk, exc
        )
        return []
    by_row: dict[str, str] = {}
    for snap in listed:
        for tag in snap.get("tags") or []:
            if str(tag).startswith("backup:") and snap.get("id"):
                by_row[str(tag)[len("backup:") :]] = str(snap["id"])
    if not by_row:
        return []
    ended = Backup.objects.filter(
        instance=instance, pk__in=[k for k in by_row if _is_uuid(k)], status__in=(BackupStatus.FAILED, BackupStatus.DELETED)
    ).exclude(job__status__in=ACTIVE_STATUSES)
    return sorted(by_row[str(b.pk)] for b in ended)


def _is_uuid(value: str) -> bool:
    import uuid

    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _game_excludes(instance: Instance) -> list[str]:
    """The game's own default exclusions (large re-creatable folders; Minecraft has none)."""
    definition = games.get_or_none(instance.game)
    return list(definition.default_backup_excludes) if definition is not None else []


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


# Failures of a restore operation that prove the server folder was not touched: restic restores
# into a staging folder (or a hidden one inside a ZFS dataset), and only a finished restore is
# swapped in. A failure after that (cleaning up the old folder) has another code.
RESTORE_UNTOUCHED = frozenset(
    {
        "restic_failed",
        "restic_missing",
        "repo_locked",
        "repo_missing",
        "snapshot_not_found",
        "invalid_snapshot",
        "invalid_path",
        "not_found",
        "target_running",
        "target_not_empty",
        "instance_busy",
    }
)
SNAPSHOT_ROLLBACK_UNTOUCHED = frozenset(
    {"snapshot_not_found", "invalid_snapshot", "snapshots_unavailable", "not_subvolume", "must_stop", "instance_busy"}
)
CONVERT_UNTOUCHED = frozenset({"snapshots_unavailable", "must_stop", "instance_busy", "not_found"})


def backup_software(backup: Backup) -> dict[str, Any] | None:
    """The software state the backup's files belong to (``run_backup`` records it). Backups from
    before that record only what an update changed: ``software_before`` (a software update) or
    ``modpack_before`` (a modpack update)."""
    meta = backup.meta or {}
    for key in ("software", "software_before", "modpack_before"):
        value = meta.get(key)
        if isinstance(value, dict) and value:
            return {k: v for k, v in value.items() if k in SOFTWARE_FIELDS or k == "game"}
    return None


def backup_game(backup: Backup) -> str | None:
    """The game the backup's files belong to; None when it recorded no software. Records from
    before game definitions (no ``game``) are Minecraft's."""
    recorded = backup_software(backup)
    if recorded is None:
        return None
    return str(recorded.get("game") or games.DEFAULT_GAME)


def require_same_game(backup: Backup, instance: Instance) -> None:
    """Refuse to restore the files and software of one game's backup into a server of another game
    (409 ``backup_game_mismatch``)."""
    game = backup_game(backup)
    current = instance.game or games.DEFAULT_GAME
    if game is not None and game != current:
        theirs, ours = games.get_or_none(game), games.get_or_none(current)
        raise ConflictError(
            f"This backup belongs to a {theirs.label if theirs else game} server and cannot be restored into a "
            f"{ours.label if ours else current} server",
            code="backup_game_mismatch",
            details={"backup_game": game, "game": current},
        )


def software_differs(backup: Backup, instance: Instance) -> dict[str, Any] | None:
    """The backup's software state when it differs from the server's (the image aside: it follows
    the Java version and the runtime image setting), else None."""
    recorded = backup_software(backup)
    if not recorded:
        return None
    current = software_state(instance)
    if all(current.get(f) == v for f, v in recorded.items() if f != "image"):
        return None
    return recorded


@register("restore")
def restore_job(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Restore a backup over the existing instance, after a safety backup.

    The files come back together with the software they belong to: when the backup was made with
    other versions (a "Before update" backup), they are set back as well, and the container is
    recreated before the server starts. A failure that left the files as they were starts a
    server that was running again; after anything unclear it is left stopped.
    """
    instance = Instance.objects.select_related("node").get(pk=job.instance_id)
    backup = Backup.objects.get(pk=job.params["backup_id"], instance=instance)
    require_local_backup(backup, instance)
    require_same_game(backup, instance)
    client = AgentClient(instance.node)
    was_running = False
    progress = Progress(RESTORE_UNTOUCHED)
    safety = None
    try:
        was_running = ensure_stopped(instance, ctx)
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
        ctx.check_cancelled()
        ctx.progress(0.5, f"Restoring '{backup.name}'", force=True)
        progress.requested()
        op = client.restore_backup(instance.id, backup.snapshot_id)
        progress.running()
        client.wait_operation(op["id"], timeout=6 * 3600, on_progress=ctx.agent_progress(0.5, 0.4))
    except Exception as exc:
        if progress.untouched(exc) and progress.stage != "safe":
            ctx.log("The restore failed before the restored files were put in place: the server's files are unchanged")
        _note_software_mismatch(exc, progress, backup, instance, ctx)
        put_back(job, instance, ctx, exc, was_running=was_running, untouched=progress.untouched(exc))
        raise
    software = _restore_software(backup, instance, ctx)
    if was_running:
        start_again(job, instance, ctx, done="The backup was restored.", recreate=bool(software))
    return {
        "restored": str(backup.id),
        "safety_backup": str(safety.id) if safety else None,
        "restarted": was_running,
        "software_restored": software,
    }


def _note_software_mismatch(exc: BaseException, progress: Progress, backup: Backup, instance: Instance, ctx: JobContext) -> None:
    """A restore cut off by a panel stop after the request went out: the node finishes it, but the
    step that sets the software back to the backup's (``_restore_software``) never runs. Say so in
    the log and in the job's error, with what to do."""
    from apps.jobs.runner import WorkerStopping

    if not isinstance(exc, WorkerStopping) or progress.stage == "safe":
        return
    recorded = software_differs(backup, instance)
    if recorded is None:
        return
    note = (
        f"The restore of '{backup.name}' may have finished on the node, so the server's files may now be the backup's, "
        f"made with {describe_software(recorded)}, while the server is still set to "
        f"{describe_software(software_state(instance))}. Do not start it like this: restore the same backup again "
        "(that also sets the software back), or set the versions in Settings → Software first."
    )
    ctx.log(f"warning: {note}")
    exc.note = note


def _restore_software(backup: Backup, instance: Instance, ctx: JobContext) -> dict[str, Any] | None:
    """Set the server's software back to what the restored backup was made with (if it differs).
    Returns {field: [old, new]} of what changed, or None."""
    require_same_game(backup, instance)  # (the views and jobs refuse it before anything is changed)
    recorded = software_differs(backup, instance)
    if recorded is None:
        return None
    before = describe_software(software_state(instance))
    changes = set_software(instance, recorded)
    if not changes:
        return None
    instance.refresh_from_db()
    ctx.log(
        f"The backup was made with {describe_software(software_state(instance))}; the server's software was set "
        f"back to it (it was {before}), so the restored world is not opened by another version"
    )
    return {f: [old, new] for f, (old, new) in changes.items()}


@register("restore_new")
def restore_new_job(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Restore a backup into a newly created instance (job runs under the source instance lock)."""
    source = Instance.objects.select_related("node").get(pk=job.instance_id)
    target = Instance.objects.select_related("node").get(pk=job.params["target_id"])
    backup = Backup.objects.get(pk=job.params["backup_id"], instance=source)
    require_same_game(backup, target)
    client = backup_agent(backup, source)
    if target.node_id != client.node.pk:
        # the snapshot is in another node's repository (the source server was migrated since):
        # restic restores on the node that holds it, so the new server is created there
        from apps.instances.services import allocate_port, assign_named_ports, ports_in_use

        fields = ["node", "game_port"]
        if target.ports and (target.game or games.DEFAULT_GAME) != games.DEFAULT_GAME:
            target.ports = assign_named_ports(games.for_instance(target), client.node, exclude=target, current=target.ports)
            target.game_port = int(target.ports[0]["host_port"])
            fields.append("ports")
        elif target.game_port in ports_in_use(client.node, exclude=target):
            target.game_port = allocate_port(client.node, exclude=target)
        target.node = client.node
        target.save(update_fields=fields)
        ctx.log(f"The backup is stored on {client.node.name}; the restored server is created there")
    ctx.progress(0.05, f"Restoring '{backup.name}' into {target.name}", force=True)
    op = client.restore_backup(source.id, backup.snapshot_id, target_instance_id=target.id)
    client.wait_operation(op["id"], timeout=6 * 3600, on_progress=ctx.agent_progress(0.05, 0.8))
    target.installed_at = timezone.now()
    target.save(update_fields=["installed_at"])
    _restore_software(backup, target, ctx)  # the new server runs the software of the backup, not of its source today
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
            ctx.check_cancelled()
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
    require_same_game(backup, instance)
    client = AgentClient(instance.node)
    was_running = False
    progress = Progress(RESTORE_UNTOUCHED)
    try:
        was_running = ensure_stopped(instance, ctx)
        ctx.check_cancelled()
        ctx.progress(0.2, f"Restoring {job.params['path']} from '{backup.name}'", force=True)
        progress.requested()
        op = client.backup_restore_path(instance.id, backup.snapshot_id, job.params["path"])
        progress.running()
        client.wait_operation(op["id"], timeout=6 * 3600, on_progress=ctx.agent_progress(0.2, 0.7))
    except Exception as exc:
        put_back(job, instance, ctx, exc, was_running=was_running, untouched=progress.untouched(exc))
        raise
    if was_running:
        start_again(job, instance, ctx, done=f"{job.params['path']} was restored.")
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
    name = job.params["name"]
    was_running = False
    progress = Progress(SNAPSHOT_ROLLBACK_UNTOUCHED)
    try:
        was_running = ensure_stopped(instance, ctx)
        ctx.progress(0.3, "Taking a safety snapshot of the current state", force=True)
        op = client.create_snapshot(instance.id, f"before rollback to {name}"[:120], "none", "pre_restore")
        client.wait_operation(op["id"], timeout=1800)
        ctx.check_cancelled()
        ctx.progress(0.6, f"Rolling back to snapshot {name}", force=True)
        progress.requested()
        op = client.restore_snapshot(instance.id, name)
        progress.running()
        done = client.wait_operation(op["id"], timeout=3600)
    except Exception as exc:
        put_back(job, instance, ctx, exc, was_running=was_running, untouched=progress.untouched(exc))
        raise
    # ZFS rollbacks report tidy-up problems (e.g. a history dataset that could not be promoted).
    warnings = [str(w) for w in (done.get("result") or {}).get("warnings") or []]
    for warning in warnings:
        ctx.log(f"Warning: {warning}")
    if was_running:
        start_again(job, instance, ctx, done=f"The server was rolled back to snapshot {name}.", span=(0.9, 0.09))
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
    was_running = False
    progress = Progress(CONVERT_UNTOUCHED)
    try:
        was_running = ensure_stopped(instance, ctx)
        ctx.check_cancelled()
        ctx.progress(0.2, CONVERT_MESSAGES.get(provider, "Preparing the server folder for snapshots"), force=True)
        progress.requested()
        op = client.convert_snapshots(instance.id)
        progress.running()
        done = client.wait_operation(op["id"], timeout=6 * 3600, on_progress=ctx.agent_progress(0.2, 0.7))
    except Exception as exc:
        put_back(job, instance, ctx, exc, was_running=was_running, untouched=progress.untouched(exc))
        raise
    if was_running:
        start_again(job, instance, ctx, done="The server folder was converted.")
    return done.get("result") or {}
