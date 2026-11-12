import logging
import os
import time
import uuid

from celery import Celery, Task, signals

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mcpanel.settings")

log = logging.getLogger(__name__)

# Periodic ticks that must never overlap (task name -> lock TTL in seconds, released when the
# run ends; the TTL only matters if a worker dies mid-run). With more than one consumer of the
# "periodic" queue, or a run slower than its interval (an unreachable node makes every agent
# call wait for its timeout), a second run would otherwise start while the first is still
# going: double-enqueued schedules, duplicate metric samples, a growing backlog.
SINGLE_FLIGHT: dict[str, int] = {
    "apps.instances.tasks.reconcile_all": 120,
    "apps.instances.tasks.scheduler_tick": 300,
    "apps.jobs.tasks.watchdog": 600,
    "apps.metrics.tasks.sample_all": 300,
    "apps.metrics.tasks.cleanup": 3600,
    "apps.modpacks.tasks.check_updates_due": 1800,
}

# The reconcile tick (every 10 s) doubles as the sign that scheduler and periodic worker do
# their work: each run records when it started, /api/health reports the age and the container
# health check (backend/container/healthcheck.py) fails when it is too old.
TICK_TASK = "apps.instances.tasks.reconcile_all"
TICK_KEY = "health:tick"


def _mark_tick() -> None:
    from django.core.cache import cache

    try:
        cache.set(TICK_KEY, time.time(), 24 * 3600)
    except Exception as exc:  # cache down: the health check reports it, the tick still runs
        log.debug("Could not record the start of the reconcile tick: %s", exc)


def tick_age() -> float | None:
    """Seconds since the last reconcile tick started; None if none is known (or the cache is down)."""
    from django.core.cache import cache

    try:
        started = cache.get(TICK_KEY)
    except Exception:
        return None
    return max(0.0, time.time() - started) if isinstance(started, (int, float)) else None


class MCPanelTask(Task):
    """Base task: skips a SINGLE_FLIGHT task while a previous run of it is still in progress."""

    def __call__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        ttl = SINGLE_FLIGHT.get(self.name)
        if not ttl:
            return super().__call__(*args, **kwargs)
        from django.core.cache import cache

        key = f"celery:single-flight:{self.name}"
        token = uuid.uuid4().hex
        try:
            acquired = cache.add(key, token, ttl)
        except Exception as exc:  # cache down: running unguarded beats silently skipping every tick
            log.warning("Single-flight lock for %s unavailable (%s); running without it", self.name, exc)
            return super().__call__(*args, **kwargs)
        if not acquired:
            log.info("Skipping %s: the previous run is still in progress", self.name)
            return None
        if self.name == TICK_TASK:
            _mark_tick()
        try:
            return super().__call__(*args, **kwargs)
        finally:
            try:
                if cache.get(key) == token:
                    cache.delete(key)
            except Exception as exc:  # the TTL releases it eventually
                log.warning("Could not release the single-flight lock for %s: %s", self.name, exc)


app = Celery("mcpanel", task_cls=MCPanelTask)
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(
    ["apps.instances", "apps.jobs", "apps.backups", "apps.content", "apps.metrics", "apps.modpacks", "apps.accounts"]
)
app.conf.imports = ("apps.jobs.runner", "apps.notifications.service")


class _QuietTicks(logging.Filter):
    """Drops Celery's INFO lines "Task ... received" / "... succeeded" of the beat ticks.

    They are about 9 of 10 log lines of an idle stack. Jobs, notifications and everything at
    WARNING or above are still logged. Only active with MCPANEL_QUIET_TICKS=1, which the single
    panel container sets for its processes (backend/container/supervise.py).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        data = getattr(record, "data", None)
        return not (record.levelno <= logging.INFO and isinstance(data, dict) and data.get("name") in SINGLE_FLIGHT)


if os.environ.get("MCPANEL_QUIET_TICKS", "").strip().lower() in {"1", "true", "yes", "on"}:
    for _name in ("celery.worker.strategy", "celery.app.trace"):
        logging.getLogger(_name).addFilter(_QuietTicks())


@signals.worker_ready.connect
def _resend_queued_jobs(sender=None, **_kwargs):  # type: ignore[no-untyped-def]
    """Send the jobs that are still QUEUED again when a worker that runs jobs is ready.

    Their messages are gone whenever Redis lost its contents: it runs without persistence inside
    the panel container, so that is every restart of the container and every restart of Redis
    (the supervisor restarts the workers after it). A duplicate message is harmless: run_job
    only claims a job that is still QUEUED. Never lets a worker fail to start.
    """
    try:
        queues = [queue.name for queue in sender.task_consumer.queues]
    except Exception:
        queues = [app.conf.task_default_queue]
    if app.conf.task_default_queue not in queues:
        return  # the periodic worker never runs jobs
    try:
        from django.db import connections

        from apps.jobs.tasks import resend_queued_jobs

        try:
            resend_queued_jobs()
        finally:
            # this is the worker's main process, which never uses the database again
            for connection in connections.all(initialized_only=True):
                if not connection.in_atomic_block:
                    connection.close()
    except Exception as exc:
        log.warning("Could not send the queued jobs again (the watchdog does it after 15 minutes): %s", exc)


@signals.worker_init.connect
def _clear_stop_flag(**_kwargs):  # type: ignore[no-untyped-def]
    """A new worker main process: no flag of an earlier process with the same id may count."""
    from apps.jobs import shutdown

    shutdown.clear()


@signals.worker_shutting_down.connect
def _flag_stop(**_kwargs):  # type: ignore[no-untyped-def]
    """The worker's main process got its stop signal: the jobs its pool runs end their waits
    (apps/jobs/shutdown.py), so the worker does not hold the container's stop for minutes."""
    from apps.jobs import shutdown

    shutdown.request_stop()
