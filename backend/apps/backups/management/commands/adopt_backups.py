"""Take over restic snapshots that the panel has no backup record of.

    docker compose exec panel python manage.py adopt_backups --instance <uuid> [--dry-run]
    docker compose exec panel python manage.py adopt_backups --all [--dry-run]

The panel knows a backup only by the row it writes when it takes one. After the panel's database
was lost and restored from an older dump (deploy/unraid/backup-db.sh), or when a job lost track
of a backup the agent finished anyway, the snapshots are still in the server's restic repository
(``<backup dir>/restic/<server id>``) but cannot be restored from the panel. This lists the
repository through the server's agent (GET /v1/instances/{id}/backups) and creates a backup row
for every snapshot that no row knows: its time, its kind (the snapshot's ``kind:`` tag) and its
size. A snapshot tagged ``backup:<id>`` gets that id back when it is free, so the row and the
snapshot stay linked as for a backup the panel took itself.

A repository belongs to a server id: the command only reads the repositories of servers that
the panel has (``--instance`` or every server with ``--all``).
"""

from __future__ import annotations

import uuid
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.backups.models import Backup, BackupKind, BackupStatus
from apps.core.errors import ApiError
from apps.instances.models import Instance
from apps.nodes.client import AgentClient

ADOPTED_WARNING = (
    "Taken over from the backup repository (manage.py adopt_backups): the panel had no record of this snapshot. "
    "Whether the world was paused while it was taken is not known."
)


def _when(value: Any) -> Any:
    try:
        return parse_datetime(str(value or ""))
    except (TypeError, ValueError):
        return None


def _tag(snapshot: dict[str, Any], prefix: str) -> str:
    for tag in snapshot.get("tags") or []:
        if isinstance(tag, str) and tag.startswith(prefix):
            return tag[len(prefix) :]
    return ""


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError):
        return None


def unknown_snapshots(instance: Instance, snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The snapshots of ``instance``'s repository that no backup row of the panel points to."""
    rows = list(Backup.objects.filter(instance=instance).values_list("pk", "snapshot_id"))
    known_ids = {str(sid) for _pk, sid in rows if sid}
    row_ids = {str(pk) for pk, _sid in rows}
    tagged = [_tag(s, "backup:") for s in snapshots if _uuid(_tag(s, "backup:"))]
    taken = {str(pk) for pk in Backup.objects.filter(pk__in=tagged).values_list("pk", flat=True)}  # by any server
    out = []
    for snap in snapshots:
        sid = str(snap.get("id") or "")
        if not sid:
            continue
        if sid in known_ids or any(k.startswith(sid) or sid.startswith(k) for k in known_ids if len(k) >= 8):
            continue
        owner = _tag(snap, "instance:")
        if owner and owner != str(instance.pk):
            continue  # another server's snapshot in this repository: not this server's backup
        if _tag(snap, "backup:") in row_ids:
            continue  # the row exists (running or failed): the watchdog settles it
        out.append({**snap, "_id_taken": _tag(snap, "backup:") in taken})
    return sorted(out, key=lambda s: str(s.get("time") or ""))


def adopt(instance: Instance, snapshot: dict[str, Any]) -> Backup:
    summary = snapshot.get("summary") or {}
    kind = _tag(snapshot, "kind:")
    if kind not in BackupKind.values:
        kind = BackupKind.MANUAL
    taken_at = _when(snapshot.get("time")) or timezone.now()
    started, ended = _when(summary.get("backup_start")), _when(summary.get("backup_end"))
    counts = [summary.get(key) for key in ("files_new", "files_changed", "files_unmodified")]
    files = sum(c or 0 for c in counts) if any(c is not None for c in counts) else summary.get("total_files_processed")
    wanted = _uuid(_tag(snapshot, "backup:"))
    fields: dict[str, Any] = {
        "instance": instance,
        "name": f"Adopted: {timezone.localtime(taken_at).strftime('%Y-%m-%d %H:%M')}",
        "kind": kind,
        "status": BackupStatus.SUCCEEDED,
        "snapshot_id": str(snapshot["id"]),
        "data_added_bytes": summary.get("data_added"),
        "total_bytes": summary.get("total_bytes_processed"),
        "files_count": files,
        "duration_seconds": (ended - started).total_seconds() if started and ended else None,
        "consistent": False,
        "warnings": [ADOPTED_WARNING],
        "finished_at": ended or taken_at,
        "meta": {
            "node_id": instance.node_id,
            "node_name": instance.node.name,
            "adopted": True,
            "adopted_at": timezone.now().isoformat(),
            "paths": list(snapshot.get("paths") or [])[:5],
        },
    }
    if wanted is not None and not snapshot.get("_id_taken"):
        fields["id"] = wanted
    with transaction.atomic():
        backup = Backup.objects.create(**fields)
        # created_at is set on insert; the backup was taken when the snapshot was
        Backup.objects.filter(pk=backup.pk).update(created_at=taken_at)
    backup.created_at = taken_at
    return backup


class Command(BaseCommand):
    help = "Create backup rows for restic snapshots of a server's repository that the panel does not know."

    def add_arguments(self, parser: CommandParser) -> None:
        which = parser.add_mutually_exclusive_group(required=True)
        which.add_argument("--instance", help="id (UUID) of the server whose repository is read")
        which.add_argument("--all", action="store_true", help="every server the panel has")
        parser.add_argument("--dry-run", action="store_true", help="only list what would be taken over")

    def handle(self, *args: Any, **options: Any) -> None:
        from apps.audit import service as audit

        if options["all"]:
            instances = list(Instance.objects.select_related("node").order_by("name"))
        else:
            pk = _uuid(options["instance"] or "")
            if pk is None:
                raise CommandError(f"'{options['instance']}' is not a server id (a UUID)")
            instances = list(Instance.objects.select_related("node").filter(pk=pk))
            if not instances:
                raise CommandError(f"There is no server with the id {pk}")
        dry = bool(options["dry_run"])
        failed = 0
        total = 0  # taken over, or found (dry run)
        for inst in instances:
            try:
                listed = AgentClient(inst.node).list_backups(inst.id)
            except ApiError as exc:
                if exc.error_code in ("repo_missing", "not_found"):
                    self.stdout.write(f"{inst.name} ({inst.id}): no backup repository")
                    continue
                failed += 1
                self.stderr.write(f"{inst.name} ({inst.id}): the repository cannot be read: {exc.detail}")
                continue
            new = unknown_snapshots(inst, listed)
            self.stdout.write(
                f"{inst.name} ({inst.id}): {len(listed)} snapshot(s) in the repository, {len(new)} unknown to the panel"
            )
            for snap in new:
                line = f"  {snap.get('time')}  {str(snap['id'])[:12]}  kind {_tag(snap, 'kind:') or 'manual'}"
                if dry:
                    total += 1
                    self.stdout.write(f"{line}  (would be taken over)")
                    continue
                backup = adopt(inst, snap)
                total += 1
                self.stdout.write(f"{line}  -> backup {backup.pk}")
            if new and not dry:
                audit.record(
                    "backup.adopted",
                    instance=inst,
                    target=inst,
                    details={"count": len(new), "snapshots": [str(s["id"])[:12] for s in new][:50]},
                )
        verb = "would take over" if dry else "took over"
        self.stdout.write(f"adopt_backups: {verb} {total} snapshot(s) of {len(instances)} server(s)")
        if failed:
            raise CommandError(f"{failed} repository/repositories could not be read (see above)")
