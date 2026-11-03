from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class JobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)


class Job(models.Model):
    """A tracked background operation (install, power action, backup, restore, ...)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=40, db_index=True)
    title = models.CharField(max_length=200)
    instance = models.ForeignKey("instances.Instance", null=True, blank=True, on_delete=models.SET_NULL, related_name="jobs")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    status = models.CharField(max_length=12, choices=JobStatus.choices, default=JobStatus.QUEUED, db_index=True)
    exclusive = models.BooleanField(default=True)
    progress = models.FloatField(default=0.0)
    message = models.CharField(max_length=500, blank=True)
    error = models.TextField(blank=True)
    error_code = models.CharField(max_length=60, blank=True)
    params = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    log = models.JSONField(default=list, blank=True)
    agent_operation_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["instance", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.kind} ({self.status})"

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def append_log(self, line: str) -> None:
        stamp = timezone.now().strftime("%H:%M:%S")
        log = list(self.log or [])
        log.append(f"{stamp} {line}")
        self.log = log[-300:]
