from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from .models import Job, JobStatus
from .runner import finish

STALE_QUEUED = timedelta(hours=2)
STALE_RUNNING = timedelta(hours=12)


@shared_task(name="apps.jobs.tasks.watchdog")
def watchdog() -> int:
    """Fail jobs that can no longer be making progress so instance locks are released."""
    now = timezone.now()
    count = 0
    for job in Job.objects.filter(status=JobStatus.QUEUED, created_at__lt=now - STALE_QUEUED):
        finish(
            job,
            JobStatus.FAILED,
            error="The job was never picked up by a worker (is the worker running?)",
            error_code="not_started",
        )
        count += 1
    for job in Job.objects.filter(status=JobStatus.RUNNING, started_at__lt=now - STALE_RUNNING):
        finish(job, JobStatus.FAILED, error="The job exceeded the maximum run time", error_code="timeout")
        count += 1
    return count
