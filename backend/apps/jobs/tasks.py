from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .models import Job, JobStatus
from .runner import INTERRUPTED_ERROR, finish, heartbeat_is_fresh, last_sign_of_life, run_job

log = logging.getLogger(__name__)

STALE_QUEUED = timedelta(hours=2)
# A queued job whose message got lost (e.g. Redis restarted without persistence, or a worker
# died holding it reserved) is dispatched again; run_job only claims QUEUED jobs, so a
# duplicate message is a no-op.
REDISPATCH_QUEUED_AFTER = timedelta(minutes=15)


def resend_queued_jobs() -> int:
    """Dispatch every job that is still QUEUED again; returns how many.

    Called when a worker that runs jobs becomes ready (mcpanel/celery.py): after a restart of the
    panel container, or of Redis alone, the queue is empty (Redis keeps nothing on disk), and
    without this a waiting job would only start when the watchdog re-sends it after
    REDISPATCH_QUEUED_AFTER. run_job only claims QUEUED jobs, so a message that was not lost
    after all makes the second one a no-op. Jobs older than STALE_QUEUED are left to the
    watchdog, which fails them as never started.
    """
    pks = list(
        Job.objects.filter(status=JobStatus.QUEUED, created_at__gte=timezone.now() - STALE_QUEUED)
        .order_by("created_at")
        .values_list("pk", flat=True)
    )
    for pk in pks:
        run_job.delay(str(pk))
    if pks:
        log.info("Sent %d queued job(s) again after the worker start: %s", len(pks), ", ".join(str(pk) for pk in pks))
    return len(pks)


def _fail_if(pk: Any, still_bad: Callable[[Job], bool], **kwargs: str) -> bool:
    """Fail the job under a row lock if ``still_bad(job)`` holds after re-reading it."""
    with transaction.atomic():
        job = Job.objects.select_for_update(skip_locked=True).filter(pk=pk).first()
        if job is None or not still_bad(job):
            return False
        finish(job, JobStatus.FAILED, **kwargs)
        return True


@shared_task(name="apps.jobs.tasks.watchdog")
def watchdog() -> int:
    """Fail jobs that can no longer be making progress so instance locks are released.

    * QUEUED for longer than STALE_QUEUED: no worker ever picked it up.
    * RUNNING without a heartbeat for MCPANEL_JOB_STALE_SECONDS: the worker running it died.
      A job with a fresh heartbeat is alive and is never failed for running long, except
      after MCPANEL_JOB_MAX_RUNTIME_HOURS (a hung handler holding the lock forever).
    """
    now = timezone.now()
    count = 0
    for pk in Job.objects.filter(status=JobStatus.QUEUED, created_at__lt=now - STALE_QUEUED).values_list("pk", flat=True):
        count += _fail_if(
            pk,
            lambda j: j.status == JobStatus.QUEUED,
            error="The job was never picked up by a worker (is the worker running?)",
            error_code="not_started",
        )
    for pk in Job.objects.filter(
        status=JobStatus.QUEUED, created_at__lt=now - REDISPATCH_QUEUED_AFTER, created_at__gte=now - STALE_QUEUED
    ).values_list("pk", flat=True):
        if cache.add(f"jobs:redispatch:{pk}", 1, int(REDISPATCH_QUEUED_AFTER.total_seconds())):
            log.info("Job %s is still queued; dispatching it again in case its message was lost", pk)
            run_job.delay(str(pk))

    for job in Job.objects.filter(status=JobStatus.RUNNING).only("pk", "heartbeat_at", "started_at"):
        if heartbeat_is_fresh(job, now):
            continue
        log.warning("Job %s has no heartbeat since %s; marking it interrupted", job.pk, last_sign_of_life(job))
        count += _fail_if(
            job.pk,
            lambda j: j.status == JobStatus.RUNNING and not heartbeat_is_fresh(j),
            error=INTERRUPTED_ERROR,
            error_code="interrupted",
        )

    max_hours = float(settings.MCPANEL_JOB_MAX_RUNTIME_HOURS or 0)
    if max_hours > 0:
        limit = now - timedelta(hours=max_hours)
        for pk in Job.objects.filter(status=JobStatus.RUNNING, started_at__lt=limit).values_list("pk", flat=True):
            count += _fail_if(
                pk,
                lambda j: j.status == JobStatus.RUNNING,
                error=f"The job exceeded the maximum run time ({max_hours:g} h) and looks hung",
                error_code="timeout",
            )
    return count
