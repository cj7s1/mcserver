from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class BackupKind(models.TextChoices):
    MANUAL = "manual", "Manual"
    SCHEDULED = "scheduled", "Scheduled"
    PRE_RESTORE = "pre_restore", "Before restore"
    PRE_UPDATE = "pre_update", "Before update"


class BackupStatus(models.TextChoices):
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    DELETED = "deleted", "Deleted"


class Backup(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instance = models.ForeignKey("instances.Instance", on_delete=models.CASCADE, related_name="backups")
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=20, choices=BackupKind.choices, default=BackupKind.MANUAL)
    status = models.CharField(max_length=12, choices=BackupStatus.choices, default=BackupStatus.RUNNING)
    engine = models.CharField(max_length=20, default="restic")
    snapshot_id = models.CharField(max_length=64, blank=True)
    data_added_bytes = models.BigIntegerField(null=True, blank=True)
    total_bytes = models.BigIntegerField(null=True, blank=True)
    files_count = models.IntegerField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)
    consistent = models.BooleanField(default=False)
    warnings = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)
    pinned = models.BooleanField(default=False, help_text="Pinned backups are never removed by retention")
    notes = models.TextField(blank=True, max_length=2000)
    job = models.ForeignKey("jobs.Job", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    verify_ok = models.BooleanField(null=True, blank=True)
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]


class RetentionPolicy(models.Model):
    """Per-instance retention for automatic (manual + scheduled) backups."""

    instance = models.OneToOneField("instances.Instance", on_delete=models.CASCADE, related_name="retention")
    keep_last = models.PositiveIntegerField(default=10)
    keep_daily = models.PositiveIntegerField(default=7)
    keep_weekly = models.PositiveIntegerField(default=4)
    keep_monthly = models.PositiveIntegerField(default=0)
    keep_safety_days = models.PositiveIntegerField(default=14, help_text="Keep pre-restore/pre-update backups this long")
    excludes = models.JSONField(default=list, blank=True, help_text="restic exclude patterns relative to the server folder")
