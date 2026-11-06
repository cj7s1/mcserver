"""Celery tasks for the periodic modpack update check (see updates.py)."""

from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

log = logging.getLogger(__name__)


@shared_task(name="apps.modpacks.tasks.check_updates_due", ignore_result=True, expires=1800)
def check_updates_due() -> int:
    """Hourly beat task: queue one staggered check per modpack server that is due."""
    from .updates import STAGGER_SECONDS, due_instances

    ids = due_instances(timezone.now())
    for i, instance_id in enumerate(ids):
        check_update.apply_async(args=[str(instance_id)], countdown=i * STAGGER_SECONDS, expires=3 * 3600)
    if ids:
        log.info("queued %d modpack update check(s)", len(ids))
    return len(ids)


@shared_task(name="apps.modpacks.tasks.check_update", ignore_result=True, soft_time_limit=180, time_limit=240)
def check_update(instance_id: str) -> str | None:
    from apps.instances.models import Instance

    from .updates import check_instance

    instance = Instance.objects.select_related("node").filter(pk=instance_id, archived_at__isnull=True).first()
    if instance is None or not instance.is_modpack:
        return None
    return check_instance(instance).status
