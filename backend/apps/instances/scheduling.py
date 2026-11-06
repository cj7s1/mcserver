"""Cron-style schedules (restart/backup/command/start/stop)."""

from __future__ import annotations

import logging
from datetime import datetime

from croniter import croniter
from django.utils import timezone

from apps.core.errors import ApiError
from apps.jobs.runner import enqueue

from .models import Instance, Schedule

log = logging.getLogger(__name__)


def validate_cron(expr: str) -> str:
    expr = " ".join(expr.split())
    if not croniter.is_valid(expr) or len(expr.split()) != 5:
        raise ValueError("Use a 5-field cron expression, e.g. '0 4 * * *' (04:00 daily)")
    return expr


def next_run(expr: str, after: datetime) -> datetime:
    local_after = timezone.localtime(after)
    return croniter(expr, local_after).get_next(datetime)


def run_due_schedules(now: datetime) -> int:
    count = 0
    for sched in Schedule.objects.filter(enabled=True).select_related("instance"):
        if sched.next_run_at is None:
            sched.next_run_at = next_run(sched.cron, now)
            sched.save(update_fields=["next_run_at"])
            continue
        if sched.next_run_at > now:
            continue
        sched.last_run_at = now
        sched.next_run_at = next_run(sched.cron, now)
        try:
            sched.last_result = execute(sched)
        except ApiError as exc:
            sched.last_result = f"Skipped: {exc.detail}"
        except Exception as exc:  # keep the scheduler alive
            log.exception("schedule %s failed", sched.pk)
            sched.last_result = f"Error: {exc}"
        sched.save(update_fields=["last_run_at", "next_run_at", "last_result"])
        count += 1
    return count


def execute(sched: Schedule) -> str:
    inst: Instance = sched.instance
    if inst.archived_at:
        return "Skipped: server archived"
    running = inst.status in ("running", "starting", "unhealthy")
    if sched.only_when_running and sched.action in ("restart", "command", "stop") and not running:
        return "Skipped: server not running"
    if sched.action in ("restart", "start", "stop"):
        from .power_warnings import clean_message
        from .services import enqueue_power

        payload = sched.payload or {}
        warn = int(payload.get("warn_minutes") or 0) if sched.action in ("restart", "stop") else 0
        job = enqueue_power(
            inst,
            sched.action,
            warn_minutes=warn,
            warn_message=clean_message(payload.get("warn_message")),
            title=f"Scheduled {sched.action}: {sched.name}",
        )
        if job.params.get("warn_minutes"):
            return f"Queued job {job.id} (players warned {job.params['warn_minutes']} min before)"
        return f"Queued job {job.id}"
    if sched.action == "backup":
        job = enqueue(
            "backup",
            title=f"Scheduled backup: {sched.name}",
            instance=inst,
            params={
                "name": sched.payload.get("name") or f"Scheduled: {sched.name}",
                "kind": "scheduled",
                "retention": sched.payload.get("retention"),
            },
        )
        return f"Queued job {job.id}"
    if sched.action == "snapshot":
        from apps.backups.handlers import schedule_snapshot_label

        # Retention ("keep") applies to this schedule's own snapshots, found by the schedule id in
        # their label. Snapshots from before ids were part of the label ("scheduled <name>") are
        # still matched by name, but only while no other snapshot schedule of this server has the
        # same name: otherwise they cannot be told apart and are left alone.
        same_name = Schedule.objects.filter(instance=inst, action="snapshot", name=sched.name).exclude(pk=sched.pk).exists()
        job = enqueue(
            "snapshot_create",
            title=f"Scheduled snapshot: {sched.name}",
            instance=inst,
            params={
                "label": schedule_snapshot_label(sched.pk, sched.name),
                "kind": "scheduled",
                "keep": sched.payload.get("keep", 24),
                "schedule_id": sched.pk,
                "legacy_label": "" if same_name else f"scheduled {sched.name}"[:120],
            },
        )
        return f"Queued job {job.id}"
    if sched.action == "command":
        from apps.nodes.client import AgentClient

        output = AgentClient(inst.node).command(inst.id, str(sched.payload.get("command", ""))[:1400])
        return f"OK: {output[:200]}"
    return f"Unknown action {sched.action}"
