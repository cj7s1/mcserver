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
    from .scheduling import run_due_schedules

    return run_due_schedules(timezone.now())
