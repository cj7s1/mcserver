"""Background job orchestration.

* ``enqueue`` creates a Job and (for exclusive jobs) atomically claims the
  instance's operation lock, so two conflicting operations can never run at once.
* ``run_job`` (Celery) executes the registered handler and always releases the lock.
* A job found already RUNNING when a worker picks it up was interrupted by a worker
  crash/restart; it is marked failed rather than blindly re-executed.
* Long waits inside a handler (``JobContext.sleep_until``) notice when the job was cancelled
  or abandoned meanwhile and raise ``JobCancelled``; the runner then keeps the status set by
  whoever stopped the job.
* A job enqueued with ``exclusive=False`` can take the instance lock later with
  ``claim_lock`` (used by power actions that first warn players for several minutes).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.console.events import broadcast
from apps.core.errors import ApiError, ConflictError

from .models import ACTIVE_STATUSES, Job, JobStatus

log = logging.getLogger(__name__)

Handler = Callable[[Job, "JobContext"], dict[str, Any] | None]
HANDLERS: dict[str, Handler] = {}

# Indirection so tests can drive long waits with a fake clock.
monotonic = time.monotonic
sleep = time.sleep
CANCEL_POLL_SECONDS = 2.0


class JobCancelled(Exception):
    """The job was cancelled or marked failed (abandoned) while its handler was still running."""

    def __init__(self, status: str) -> None:
        super().__init__(f"the job is {status}")
        self.status = status


def register(kind: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return deco


def job_payload(job: Job) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "kind": job.kind,
        "title": job.title,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "error": job.error,
        "error_code": job.error_code,
        "instance_id": str(job.instance_id) if job.instance_id else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


class JobContext:
    def __init__(self, job: Job) -> None:
        self.job = job
        self._last_save = 0.0

    def progress(self, value: float | None = None, message: str | None = None, *, force: bool = False) -> None:
        job = self.job
        if value is not None:
            job.progress = max(0.0, min(1.0, float(value)))
        if message and message != job.message:
            job.message = message[:500]
            job.append_log(message)
        now = time.monotonic()
        if force or now - self._last_save > 1.0:
            self._last_save = now
            Job.objects.filter(pk=job.pk).update(progress=job.progress, message=job.message, log=job.log)
            broadcast("job.progress", job_payload(job), str(job.instance_id) if job.instance_id else None)

    def log(self, line: str) -> None:
        self.job.append_log(line)
        Job.objects.filter(pk=self.job.pk).update(log=self.job.log)

    def check_cancelled(self) -> None:
        """Raise JobCancelled if someone cancelled or abandoned this job meanwhile."""
        status = Job.objects.filter(pk=self.job.pk).values_list("status", flat=True).first()
        if status != JobStatus.RUNNING:
            raise JobCancelled(status or "deleted")

    def sleep_until(self, deadline: float) -> None:
        """Wait until ``deadline`` (a ``runner.monotonic()`` value), checking for cancellation."""
        while True:
            self.check_cancelled()
            remaining = deadline - monotonic()
            if remaining <= 0:
                return
            sleep(min(remaining, CANCEL_POLL_SECONDS))

    def agent_progress(self, start: float, span: float) -> Callable[[dict[str, Any]], None]:
        """Adapter mapping an agent operation's progress into this job's progress range."""

        def _cb(op: dict[str, Any]) -> None:
            self.progress(start + span * float(op.get("progress") or 0.0), op.get("message") or None)

        return _cb


def enqueue(
    kind: str,
    *,
    title: str,
    instance: Any = None,
    user: Any = None,
    params: dict[str, Any] | None = None,
    exclusive: bool = True,
) -> Job:
    from apps.instances.models import Instance

    if kind not in HANDLERS:
        raise ValueError(f"No handler registered for job kind {kind!r}")
    with transaction.atomic():
        if instance is not None and exclusive:
            locked = Instance.objects.select_for_update().get(pk=instance.pk)
            if locked.active_job_id:
                current = Job.objects.filter(pk=locked.active_job_id, status__in=ACTIVE_STATUSES).first()
                if current is not None:
                    raise ConflictError(
                        f"Another operation is in progress: {current.title}",
                        code="instance_busy",
                        details={"job_id": str(current.id), "kind": current.kind},
                    )
            job = Job.objects.create(kind=kind, title=title, instance=locked, user=user, params=params or {}, exclusive=True)
            locked.active_job = job
            locked.save(update_fields=["active_job"])
        else:
            job = Job.objects.create(kind=kind, title=title, instance=instance, user=user, params=params or {}, exclusive=False)
        transaction.on_commit(lambda: run_job.delay(str(job.id)))
    broadcast("job.created", job_payload(job), str(job.instance_id) if job.instance_id else None)
    return job


def claim_lock(job: Job, ctx: JobContext, *, wait: float = 0.0, poll: float = 5.0) -> None:
    """Take the instance lock for a job that was enqueued with ``exclusive=False``.

    Waits up to ``wait`` seconds for another exclusive job to finish (cancellation-aware) and
    raises ``ConflictError(instance_busy)`` after that. No-op for jobs that already hold it.
    """
    from apps.instances.models import Instance

    if job.exclusive or not job.instance_id:
        return
    deadline = monotonic() + wait
    waiting_for = None
    while True:
        with transaction.atomic():
            status = Job.objects.select_for_update().filter(pk=job.pk).values_list("status", flat=True).first()
            if status != JobStatus.RUNNING:
                raise JobCancelled(status or "deleted")
            locked = Instance.objects.select_for_update().get(pk=job.instance_id)
            other = None
            if locked.active_job_id and locked.active_job_id != job.pk:
                other = Job.objects.filter(pk=locked.active_job_id, status__in=ACTIVE_STATUSES).first()
            if other is None:
                locked.active_job = job
                locked.save(update_fields=["active_job"])
                Job.objects.filter(pk=job.pk).update(exclusive=True)
                job.exclusive = True
                return
        if monotonic() >= deadline:
            raise ConflictError(
                f"Another operation is in progress: {other.title}",
                code="instance_busy",
                details={"job_id": str(other.id), "kind": other.kind},
            )
        if waiting_for != other.pk:
            waiting_for = other.pk
            ctx.progress(None, f"Waiting for '{other.title}' to finish", force=True)
        ctx.sleep_until(min(deadline, monotonic() + poll))


def _release(job: Job) -> None:
    from apps.instances.models import Instance

    if job.instance_id and job.exclusive:
        Instance.objects.filter(pk=job.instance_id, active_job_id=job.pk).update(active_job=None)


def finish(job: Job, status: str, *, error: str = "", error_code: str = "", result: dict[str, Any] | None = None) -> None:
    job.status = status
    job.finished_at = timezone.now()
    job.error = error
    job.error_code = error_code
    if result is not None:
        job.result = result
    if status == JobStatus.SUCCEEDED:
        job.progress = 1.0
    if error:
        job.append_log(f"ERROR: {error}")
    job.save(update_fields=["status", "finished_at", "error", "error_code", "result", "progress", "log", "message"])
    _release(job)
    broadcast("job.finished", job_payload(job), str(job.instance_id) if job.instance_id else None)
    _notify_job(job)


def _notify_job(job: Job) -> None:
    from apps.notifications.service import notify

    instance = job.instance if job.instance_id else None
    data = {"job": job.title, "status": job.status, "error": job.error[:500]}
    if job.status == JobStatus.FAILED:
        notify("job.failed", f"Failed: {job.title}", job.error[:1500] or "The job failed.", instance=instance, data=data)
        if job.kind == "backup":
            notify("backup.failed", f"Backup failed: {job.title}", job.error[:1500], instance=instance, data=data)
    elif job.status == JobStatus.SUCCEEDED and job.kind == "backup":
        notify(
            "backup.succeeded",
            f"Backup completed: {job.title}",
            "The backup finished successfully.",
            instance=instance,
            data=data,
        )


@shared_task(name="apps.jobs.runner.run_job", acks_late=True)
def run_job(job_id: str) -> None:
    with transaction.atomic():
        job = Job.objects.select_for_update().filter(pk=job_id).first()
        if job is None:
            return
        if job.status == JobStatus.RUNNING:
            log.warning("Job %s was interrupted (worker restart); marking failed", job_id)
            finish(
                job,
                JobStatus.FAILED,
                error="The worker restarted while this operation was running. Check the server state before retrying.",
                error_code="interrupted",
            )
            return
        if job.status != JobStatus.QUEUED:
            return
        job.status = JobStatus.RUNNING
        job.started_at = timezone.now()
        job.save(update_fields=["status", "started_at"])

    ctx = JobContext(job)
    handler = HANDLERS.get(job.kind)
    if handler is None:
        # e.g. the API was upgraded before the worker: fail cleanly so the instance lock is released
        log.error("No handler for job kind %s (worker out of date?)", job.kind)
        finish(
            job,
            JobStatus.FAILED,
            error=f"This worker does not know how to run '{job.kind}' jobs. "
            "Restart/upgrade the worker so it matches the API version.",
            error_code="unknown_job_kind",
        )
        return
    try:
        result = handler(job, ctx) or {}
    except JobCancelled as exc:
        # finished by the cancel/abandon request already; only make sure the lock is free
        log.info("Job %s (%s) stopped early: %s", job.id, job.kind, exc)
        _release(job)
    except ApiError as exc:
        log.info("Job %s (%s) failed: %s", job.id, job.kind, exc)
        finish(job, JobStatus.FAILED, error=str(exc.detail), error_code=exc.error_code)
    except Exception as exc:
        log.exception("Job %s (%s) crashed", job.id, job.kind)
        finish(job, JobStatus.FAILED, error=f"Unexpected error: {exc}", error_code="internal_error")
    else:
        finish(job, JobStatus.SUCCEEDED, result=result)
    finally:
        if job.instance_id:
            try:
                from apps.instances.reconcile import reconcile_instance

                reconcile_instance(job.instance_id)
            except Exception as exc:
                log.debug("post-job reconcile failed: %s", exc)
