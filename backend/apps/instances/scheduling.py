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
        job = enqueue(sched.action, title=f"Scheduled {sched.action}: {sched.name}", instance=inst)
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
    if sched.action == "command":
        from apps.nodes.client import AgentClient

        output = AgentClient(inst.node).command(inst.id, str(sched.payload.get("command", ""))[:1400])
        return f"OK: {output[:200]}"
    return f"Unknown action {sched.action}"
