"""Background job orchestration.

* ``enqueue`` creates a Job and (for exclusive jobs) atomically claims the
  instance's operation lock, so two conflicting operations can never run at once.
* ``run_job`` (Celery) executes the registered handler and always releases the lock.
* While the handler runs, a background thread touches ``Job.heartbeat_at`` (and so does every
  progress/log update). A message for a job that is already RUNNING is a redelivery: with a
  fresh heartbeat the job is still running on another worker (e.g. the broker's visibility
  timeout expired during a long job) and the message is ignored; with a stale heartbeat the
  worker that ran it crashed/restarted, so it is marked failed ("interrupted") rather than
  blindly re-executed. The watchdog (``tasks.watchdog``) fails stale jobs without a redelivery.
* Long waits inside a handler (``JobContext.sleep_until`` and, through
  ``apps.nodes.client.operation_guard``, every ``AgentClient.wait_operation``) notice when the job
  was cancelled or abandoned meanwhile and raise ``JobCancelled``; the runner then keeps the
  status set by whoever stopped the job. When the worker is being stopped they raise
  ``WorkerStopping`` (apps/jobs/shutdown.py) and the job is failed as interrupted at once.
* ``finish`` only ends a job that is still queued or running: a handler that returns after its
  job was cancelled or marked as failed does not turn it "succeeded".
* A job enqueued with ``exclusive=False`` can take the instance lock later with
  ``claim_lock`` (used by power actions that first warn players for several minutes).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from celery import shared_task
from django.conf import settings
from django.db import connections, transaction
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


class WorkerStopping(JobCancelled):
    """The worker running the job is being stopped (apps/jobs/shutdown.py): the job ends now and
    is failed as interrupted, instead of holding the worker until it is killed."""

    def __init__(self) -> None:
        super().__init__(JobStatus.RUNNING)
        self.args = ("the worker is stopping",)
        # what a handler adds to the job's error (WORKER_STOPPING_ERROR) about the state it leaves
        self.note = ""


WORKER_STOPPING_ERROR = (
    "The panel was being stopped (or restarted) while this operation ran, so the job ended here. "
    "The node's agent finishes a step it had started; check the server state before retrying."
)


def heartbeat_interval() -> float:
    return float(settings.MCPANEL_JOB_HEARTBEAT_SECONDS)


def stale_after() -> timedelta:
    """How long a RUNNING job may go without a heartbeat before it counts as interrupted."""
    return timedelta(seconds=float(settings.MCPANEL_JOB_STALE_SECONDS))


def last_sign_of_life(job: Job) -> datetime | None:
    # heartbeat_at is set when the job is claimed; started_at covers rows from older versions.
    # Neither set (should not happen for a RUNNING job) counts as stale.
    return job.heartbeat_at or job.started_at


def heartbeat_is_fresh(job: Job, now: datetime | None = None) -> bool:
    last = last_sign_of_life(job)
    return last is not None and (now or timezone.now()) - last < stale_after()


def touch_heartbeat(job_id: Any) -> bool:
    """Record that the job's worker is alive. Returns False once the job is no longer RUNNING."""
    return Job.objects.filter(pk=job_id, status=JobStatus.RUNNING).update(heartbeat_at=timezone.now()) > 0


class _Heartbeat:
    """Touches the job's heartbeat from a daemon thread while the handler runs.

    Handlers block for long stretches inside single agent calls (streamed uploads, stops,
    waits without progress callbacks), so progress updates alone are not a reliable sign of
    life. The thread uses its own database connection and closes it when it ends.
    """

    def __init__(self, job_id: Any, interval: float) -> None:
        self.job_id = job_id
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _Heartbeat:
        if self.interval > 0:
            self._thread = threading.Thread(target=self._run, name=f"job-heartbeat-{self.job_id}", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.interval):
                try:
                    if not touch_heartbeat(self.job_id):
                        return  # finished, cancelled or abandoned meanwhile
                except Exception as exc:  # e.g. database briefly unreachable: keep trying
                    log.warning("job %s heartbeat failed: %s", self.job_id, exc)
        finally:
            connections.close_all()  # only this thread's connections


def register(kind: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return deco


def _broadcast_job(event: str, job: Job) -> None:
    """A job event: to the users who see its server; a job without one (a deleted server's) only to the user who
    started it and to users who see every server (the suspicion of slice D: it went to every signed-in user)."""
    if job.instance_id:
        broadcast(event, job_payload(job), str(job.instance_id))
    else:
        broadcast(event, job_payload(job), None, owner_id=str(job.user_id) if job.user_id else None)


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
        self._last_check: float | None = None

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
            Job.objects.filter(pk=job.pk).update(
                progress=job.progress, message=job.message, log=job.log, heartbeat_at=timezone.now()
            )
            _broadcast_job("job.progress", job)

    def log(self, line: str) -> None:
        self.job.append_log(line)
        Job.objects.filter(pk=self.job.pk).update(log=self.job.log, heartbeat_at=timezone.now())

    def check_cancelled(self) -> None:
        """Raise JobCancelled if someone cancelled or abandoned this job meanwhile, WorkerStopping
        when the worker is being stopped."""
        from .shutdown import stop_requested

        if stop_requested():
            raise WorkerStopping()
        status = Job.objects.filter(pk=self.job.pk).values_list("status", flat=True).first()
        if status != JobStatus.RUNNING:
            raise JobCancelled(status or "deleted")
        self._last_check = monotonic()

    def guard(self) -> None:
        """``check_cancelled`` for polling loops (``AgentClient.wait_operation``): the worker's stop
        is noticed at once, the job's status is read at most every CANCEL_POLL_SECONDS."""
        from .shutdown import stop_requested

        if stop_requested():
            raise WorkerStopping()
        if self._last_check is None or monotonic() - self._last_check >= CANCEL_POLL_SECONDS:
            self.check_cancelled()

    def remember(self, **facts: Any) -> None:
        """Keep facts about what the job did to its server in ``Job.result`` right away (the
        handler's own result replaces them when it ends). The watchdog reads them when the job
        was cut off: ``stopped_server`` = the job stopped a running server."""
        current = Job.objects.filter(pk=self.job.pk).values_list("result", flat=True).first() or {}
        merged = {**current, **facts}
        Job.objects.filter(pk=self.job.pk, status=JobStatus.RUNNING).update(result=merged)
        self.job.result = {**(self.job.result or {}), **facts}

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
        transaction.on_commit(lambda: dispatch(job.id))
    _broadcast_job("job.created", job)
    return job


def dispatch(job_id: Any) -> bool:
    """Send the message that makes a worker run the job. Returns False when it could not be sent.

    The job exists and is QUEUED at this point (and holds its server, if it is exclusive), so a
    broker that is not reachable for a moment (Redis is being restarted) must not turn into an
    error for whoever queued it: that request would answer 500 for a job that then runs anyway.
    The job stays queued and its message is sent again: when the job worker starts
    (``tasks.resend_queued_jobs``, the supervisor restarts the workers after Redis) and by the
    watchdog after ``tasks.REDISPATCH_QUEUED_AFTER``.
    """
    try:
        run_job.delay(str(job_id))
        return True
    except Exception as exc:
        log.warning(
            "Job %s is queued, but its message could not be sent (%s: %s); it is sent again", job_id, type(exc).__name__, exc
        )
        return False


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


FINISH_FIELDS = ("status", "finished_at", "error", "error_code", "result", "progress", "log", "message")


def finish(job: Job, status: str, *, error: str = "", error_code: str = "", result: dict[str, Any] | None = None) -> bool:
    """End a job that is still queued or running. Returns False (and changes nothing but the
    lock) when it has ended already: cancelled, marked as failed, failed by the watchdog. Its
    handler may still return later, and must not turn it "succeeded", nor the other way round."""
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
    ended = Job.objects.filter(pk=job.pk, status__in=ACTIVE_STATUSES).update(**{f: getattr(job, f) for f in FINISH_FIELDS})
    _release(job)
    if not ended:
        log.info("Job %s had ended already; its status is kept", job.pk)
        job.refresh_from_db()
        return False
    _broadcast_job("job.finished", job)
    _notify_job(job)
    return True


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


INTERRUPTED_ERROR = (
    "The worker running this operation stopped (crashed or restarted) before it finished. Check the server state before retrying."
)
LEFT_STOPPED_NOTE = (
    " It had stopped the server for its work, and the server was left stopped: check its files and start it when it is fine."
)


def settle_interrupted(job: Job) -> None:
    """After a job was failed as interrupted: a server the job had stopped for its work
    (``Job.result["stopped_server"]``, see ``instances.handlers.ensure_stopped``) and that is not
    running now is recorded as stopped, and the job's error says so. Whether the job got to the
    step that changes the server's files is not known, so it is not started again. Never raises."""
    from apps.instances.models import Instance

    try:
        if not job.instance_id or not (job.result or {}).get("stopped_server"):
            return
        Instance.objects.filter(pk=job.instance_id, desired_state="running").exclude(container_state="running").update(
            desired_state="stopped", resume_after_boot=False, resume_flagged_at=None
        )
        Job.objects.filter(pk=job.pk).update(error=job.error + LEFT_STOPPED_NOTE)
    except Exception as exc:
        log.warning("Could not settle the server of interrupted job %s: %s", job.pk, exc)


@shared_task(name="apps.jobs.runner.run_job", acks_late=True)
def run_job(job_id: str) -> None:
    with transaction.atomic():
        # The row lock is what makes a second message for the same job harmless, and queued jobs
        # are sent again on purpose (dispatch, tasks.resend_queued_jobs, the watchdog): two workers
        # that take the two messages at the same moment must not both find the job QUEUED.
        # tests/test_jobs.py runs exactly that with two threads.
        job = Job.objects.select_for_update().filter(pk=job_id).first()
        if job is None:
            return
        if job.status == JobStatus.RUNNING:
            if heartbeat_is_fresh(job):
                # Redelivered message (e.g. the broker's visibility timeout expired during a long
                # job) while the job is still alive on another worker: leave it alone.
                log.warning("Job %s is still running on another worker (redelivered message ignored)", job_id)
                return
            log.warning("Job %s was interrupted (no heartbeat since %s); marking failed", job_id, last_sign_of_life(job))
            if finish(job, JobStatus.FAILED, error=INTERRUPTED_ERROR, error_code="interrupted"):
                settle_interrupted(job)
            return
        if job.status != JobStatus.QUEUED:
            return
        job.status = JobStatus.RUNNING
        job.started_at = job.heartbeat_at = timezone.now()
        job.save(update_fields=["status", "started_at", "heartbeat_at"])

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
    from apps.nodes.client import operation_guard

    guard_token = operation_guard.set(ctx.guard)
    try:
        with _Heartbeat(job.pk, heartbeat_interval()):
            result = handler(job, ctx) or {}
    except WorkerStopping as stopping:
        log.warning("Job %s (%s) ended because the worker is stopping", job.id, job.kind)
        error = f"{WORKER_STOPPING_ERROR} {stopping.note}" if stopping.note else WORKER_STOPPING_ERROR
        try:
            if finish(job, JobStatus.FAILED, error=error, error_code="interrupted"):
                settle_interrupted(job)
        except Exception as exc:  # the database may be going away too: the watchdog fails it later
            log.warning("Could not record the end of job %s (%s); the watchdog does it", job.id, exc)
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
        operation_guard.reset(guard_token)
        if job.instance_id:
            try:
                from apps.instances.reconcile import reconcile_instance

                reconcile_instance(job.instance_id)
            except Exception as exc:
                log.debug("post-job reconcile failed: %s", exc)
