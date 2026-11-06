import logging
import os
import uuid

from celery import Celery, Task

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


class MCPanelTask(Task):
    """Base task: skips a SINGLE_FLIGHT task while a previous run of it is still in progress."""

    def __call__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        ttl = SINGLE_FLIGHT.get(self.name)
        if not ttl:
            return super().__call__(*args, **kwargs)
        from django.core.cache import cache

        key = f"celery:single-flight:{self.name}"
        token = uuid.uuid4().hex
        if not cache.add(key, token, ttl):
            log.info("Skipping %s: the previous run is still in progress", self.name)
            return None
        try:
            return super().__call__(*args, **kwargs)
        finally:
            if cache.get(key) == token:
                cache.delete(key)


app = Celery("mcpanel", task_cls=MCPanelTask)
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(["apps.instances", "apps.jobs", "apps.backups", "apps.content", "apps.metrics", "apps.modpacks"])
app.conf.imports = ("apps.jobs.runner", "apps.notifications.service")
