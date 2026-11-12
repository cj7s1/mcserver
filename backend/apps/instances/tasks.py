from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

from apps.nodes.models import Node

from .reconcile import reconcile_instance, reconcile_node

log = logging.getLogger(__name__)


@shared_task(name="apps.instances.tasks.reconcile_all", ignore_result=True, expires=30)
def reconcile_all() -> dict:
    results = {}
    for node in Node.objects.all():
        results[node.name] = reconcile_node(node)
    return results


@shared_task(name="apps.instances.tasks.reconcile_one", ignore_result=True)
def reconcile_one(instance_id: str) -> None:
    reconcile_instance(instance_id)


@shared_task(name="apps.instances.tasks.scheduler_tick", ignore_result=True, expires=55)
def scheduler_tick() -> int:
    from .scheduling import resume_flagged_servers, run_due_schedules

    now = timezone.now()
    try:
        # servers stopped by `manage.py host_shutdown`: at most one is started per tick
        resume_flagged_servers(now)
    except Exception:  # never let it keep the schedules from running
        log.exception("resuming servers after a host shutdown failed")
    return run_due_schedules(now)


@shared_task(name="apps.instances.tasks.idle_stop_tick", ignore_result=True, expires=55)
def idle_stop_tick() -> int:
    """Stop when empty (B2): stops the servers that had no players for their ``idle_stop_minutes``."""
    from .idle import check_idle

    return check_idle()
