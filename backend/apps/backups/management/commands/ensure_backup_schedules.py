"""Give every server a nightly backup schedule.

    docker compose exec panel python manage.py ensure_backup_schedules [--dry-run]

Backups only happen through a per-server schedule, and servers made before the create wizard
offered one have none. This adds the default "Nightly backup" schedule to every server that is not
archived and has no enabled backup schedule: daily between 03:00 and 04:59 panel time, at a minute
taken from the server's id that no other schedule of that server uses (two schedules due in the same
minute do not both run). See apps.instances.services.add_nightly_backup_schedule.

One line is printed per server and every addition is audited (no actor: shown as "system").
Running it again changes nothing; ``--dry-run`` only reports.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.audit import service as audit
from apps.instances import services
from apps.instances.models import Instance, Schedule
from apps.instances.serializers import ScheduleSerializer


class Command(BaseCommand):
    help = "Add a nightly backup schedule to every server that has no enabled backup schedule"

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("--dry-run", action="store_true", help="only report what would be added, change nothing")

    def handle(self, *args, **opts):  # type: ignore[no-untyped-def]
        dry_run = opts["dry_run"]
        added = kept = archived = 0
        for pk in Instance.objects.order_by("name", "pk").values_list("pk", flat=True):
            with transaction.atomic():
                # the row lock makes two runs at the same time take turns instead of both adding one
                instance = Instance.objects.select_for_update().filter(pk=pk).first()
                if instance is None:  # deleted since the list was read
                    continue
                label = f"{instance.name} ({instance.pk})"
                if instance.archived_at:
                    archived += 1
                    self.stdout.write(f"{label}: archived, skipped")
                    continue
                backups = list(Schedule.objects.filter(instance=instance, action="backup").order_by("name", "pk"))
                enabled = [s for s in backups if s.enabled]
                if enabled:
                    kept += 1
                    names = ", ".join(f"'{s.name}' ({s.cron})" for s in enabled)
                    self.stdout.write(f"{label}: already has backup schedule {names}, left as it is")
                    continue
                taken = services.schedule_minutes(instance)
                cron = services.nightly_backup_cron(instance.pk, taken)
                minute, hour = (int(part) for part in cron.split()[:2])
                notes = ""
                if backups:
                    notes += f"; {len(backups)} paused backup schedule(s) left paused"
                if minute in taken:
                    notes += "; every minute is used by this server's other schedules, so a run can be skipped as busy"
                if not dry_run:
                    schedule = services.add_nightly_backup_schedule(instance)
                    audit.record(
                        "schedule.created",
                        instance=instance,
                        target=schedule,
                        details={**ScheduleSerializer(schedule).data, "via": "ensure_backup_schedules"},
                    )
                added += 1
                verb = "would add" if dry_run else "added"
                self.stdout.write(
                    f"{label}: {verb} '{services.NIGHTLY_BACKUP_NAME}', daily at {hour:02d}:{minute:02d} ({cron}){notes}"
                )
        self.stdout.write(
            f"{'Would add' if dry_run else 'Added'} {added} schedule(s); {kept} server(s) already had one; "
            f"{archived} archived server(s) skipped. Times are panel time (TZ={settings.TIME_ZONE})."
        )
        if dry_run:
            self.stdout.write("Dry run: nothing was changed.")
