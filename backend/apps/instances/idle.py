"""Stop when empty (B2 of the feature plan of 2026-11-12).

A server with ``idle_stop_minutes`` > 0 that runs, has been ready for at least that long (and at least
GRACE_MINUTES), and had no players in every player-count sample of that whole time, is stopped through the
ordinary stop job (the game saves), audited as ``instance.idle_stopped`` ("stopped: empty for N minutes") with
the notification event of the same name.

Never while a job runs for the server (the stop job takes the server's lock; a busy server is skipped), never
while the count is unknown or stale: the decision reads the samples the metrics task takes every minute
(apps.metrics.tasks.sample_all); a sample whose count was unknown, a gap of more than MAX_GAP seconds between
samples, or a newest sample older than MAX_AGE seconds means "do not stop". Games whose count the panel cannot
read (``player_count_unavailable``, Valheim) are never stopped this way; the setting is refused for them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from django.utils import timezone

from apps import games

from .models import DesiredState, Instance, Status

log = logging.getLogger(__name__)
GRACE_MINUTES = 5  # a server is at least this long ready before it can be stopped as empty
MAX_GAP = 180  # seconds between two samples (they come every minute) above which the count was not known
MAX_AGE = 180  # seconds: the newest sample must be younger than this
MIN_MINUTES, MAX_MINUTES = 5, 24 * 60


def unavailable_reason(instance: Instance) -> str | None:
    """Why stop-when-empty cannot be offered for this server (None: it can)."""
    definition = games.get_or_none(instance.game or games.DEFAULT_GAME)
    if definition is None:
        return "Unknown game"
    return definition.player_count_unavailable(instance)


def empty_since_window(instance: Instance, now: datetime) -> bool:
    """Whether every player-count sample of the last ``idle_stop_minutes`` says 0, the samples cover that time
    without a gap and the newest is fresh."""
    from apps.metrics.models import MetricSample

    minutes = int(instance.idle_stop_minutes or 0)
    start = now - timedelta(minutes=minutes)
    samples = list(
        MetricSample.objects.filter(instance=instance, taken_at__gte=start - timedelta(seconds=MAX_GAP))
        .order_by("taken_at")
        .values_list("taken_at", "players_online")
    )
    if not samples or (now - samples[-1][0]).total_seconds() > MAX_AGE:
        return False  # stale: the count is not known now
    covering = [s for s in samples if s[0] >= start]
    earlier = [s for s in samples if s[0] < start]
    # the window must be covered from its start: a sample just before it, or the first inside it close to it
    previous = earlier[-1][0] if earlier else None
    if previous is None and (not covering or (covering[0][0] - start).total_seconds() > MAX_GAP):
        return False
    times = ([previous] if previous is not None else []) + [t for t, _p in covering]
    if any((b - a).total_seconds() > MAX_GAP for a, b in pairwise(times)):
        return False
    counts = [p for _t, p in (earlier[-1:] + covering)]
    return all(p == 0 for p in counts)  # (None: unknown, never counts as empty)


def due(instance: Instance, now: datetime) -> bool:
    """Whether ``instance`` is to be stopped as empty now (without asking the agent)."""
    minutes = int(instance.idle_stop_minutes or 0)
    if minutes <= 0 or instance.archived_at is not None or instance.active_job_id is not None:
        return False
    if instance.status != Status.RUNNING or instance.desired_state != DesiredState.RUNNING:
        return False
    if instance.ready_at is None or now - instance.ready_at < timedelta(minutes=max(minutes, GRACE_MINUTES)):
        return False
    if unavailable_reason(instance) is not None:
        return False
    from .power_warnings import pending_countdown

    if pending_countdown(instance) is not None:
        return False  # the owner's restart or stop is counting down: never superseded by an idle stop (F-16)
    return empty_since_window(instance, now)


def stop_idle(instance: Instance, now: datetime) -> Any:
    """Queue the ordinary stop job of an empty server, audit it and notify. None when it is busy now."""
    from apps.audit import service as audit
    from apps.core.errors import ConflictError
    from apps.notifications.service import notify

    from .services import enqueue_power

    minutes = int(instance.idle_stop_minutes)
    try:
        job = enqueue_power(instance, "stop", user=None, title=f"Stop (empty for {minutes} minutes)")
    except ConflictError:
        return None  # a job started meanwhile: it is not idle
    reason = f"stopped: empty for {minutes} minutes"
    audit.record(
        "instance.idle_stopped",
        instance=instance,
        target=instance,
        details={"minutes": minutes, "reason": reason, "job": str(job.id)},
    )
    notify(
        "instance.idle_stopped",
        f"{instance.name} was stopped",
        f"{instance.name} was stopped because nobody played on it for {minutes} minutes (stop when empty).",
        instance=instance,
        data={"status": "stopped", "minutes": minutes},
    )
    log.info("stopping %s: empty for %s minutes", instance.pk, minutes)
    return job


def check_idle(now: datetime | None = None) -> int:
    """One pass over the servers with the setting on; returns how many were stopped. One ``try`` per server."""
    now = now or timezone.now()
    stopped = 0
    candidates = Instance.objects.select_related("node").filter(
        idle_stop_minutes__gt=0, status=Status.RUNNING, desired_state=DesiredState.RUNNING, archived_at__isnull=True
    )
    for instance in candidates:
        try:
            if due(instance, now) and stop_idle(instance, now) is not None:
                stopped += 1
        except Exception:  # one server must never keep the others from being checked
            log.exception("stop-when-empty check of %s failed", instance.pk)
    return stopped
