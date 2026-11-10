"""Cron-style schedules (restart/backup/command/start/stop) and the restart of servers after a host shutdown."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from croniter import croniter
from django.db import transaction
from django.utils import timezone

from apps.core.errors import ApiError, ConflictError
from apps.jobs.runner import enqueue

from .models import DesiredState, Instance, Schedule

log = logging.getLogger(__name__)

# Agent timeout of the resume step: it runs inside the scheduler tick and must not hold it up.
RESUME_AGENT_TIMEOUT = 10.0
# A mark that this same panel runtime has seen waiting for longer than this is given up: Docker
# was not stopped after all (`manage.py host_shutdown` or the hook was run by hand). A real
# shutdown ends the panel within a minute or two of the mark.
RESUME_STALE_AFTER = timedelta(minutes=10)
# A job of one of these kinds that failed says nothing about whether the server may be started:
# it only reads the server's files (backups, snapshots, the source of a clone or of a restore
# into a new server), or it only creates and starts the container, which the resume step's own
# start does again. Every other kind changes the server's files (restores, world and modpack
# operations, updates, a move to another node), and after its failure the server is left
# stopped on purpose. A kind that is not listed counts as one of those.
RESUME_HARMLESS_JOB_KINDS = frozenset(
    {
        "backup",
        "backup_delete",
        "backup_verify",
        "snapshot_create",
        "snapshot_delete",
        "restore_new",
        "clone",
        "install",
        "start",
        "restart",
        "recreate",
    }
)
# A job that failed with one of these codes never ran its handler, whatever its kind: it was
# still queued when the watchdog gave up on it ("not_started", typically after the host was down
# for hours), or no worker knew its kind. It did nothing to the server, so it says nothing about
# whether the server may be started.
RESUME_HARMLESS_ERROR_CODES = frozenset({"not_started", "unknown_job_kind"})
NOT_RESUMED_AUDIT = "instance.not_resumed_after_boot"
_LOADED_AT = timezone.now()


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


# --- start servers again after a host shutdown ----------------------------------------------------
def runtime_started_at() -> datetime:
    """When the container this process runs in was started (the start time of its PID 1).

    Docker starts every container of the panel again after a host reboot or an array
    stop/start, so this is later than a ``resume_flagged_at`` written before Docker went down,
    and earlier than one written by ``host_shutdown`` while this container keeps running.
    It must be PID 1 and not this process: the scheduler tick runs in a worker process, and the
    supervisor starts workers again while the container keeps running (after a crash, after a
    restart of Redis), which would make a mark of a shutdown that is still going on look old.
    Without a readable /proc the load time of this module is used.
    """
    try:
        with open("/proc/1/stat", encoding="ascii", errors="replace") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
        ticks = int(fields[19])  # field 22 of proc(5) stat: start time in clock ticks since the host booted
        with open("/proc/stat", encoding="ascii", errors="replace") as fh:
            boot = next(int(line.split()[1]) for line in fh if line.startswith("btime "))
        return datetime.fromtimestamp(boot + ticks / os.sysconf("SC_CLK_TCK"), tz=UTC)
    except (OSError, ValueError, IndexError, StopIteration):
        return _LOADED_AT


def _clear_resume(instance: Instance) -> bool:
    """Drop the mark; False when something else (an explicit action) cleared it meanwhile."""
    return bool(
        Instance.objects.filter(pk=instance.pk, resume_after_boot=True).update(resume_after_boot=False, resume_flagged_at=None)
    )


def _live(container: dict[str, Any] | None) -> bool:
    c = container or {}
    return bool(c.get("running") or c.get("restarting") or c.get("status") == "restarting")


def _job_that_left_it_stopped(instance: Instance, flagged_at: datetime | None) -> Any:
    """The newest job that held this server, ended after it was marked and did not succeed
    (failed, marked as failed by a user, interrupted, cancelled), or None.

    After such a job the server is stopped on purpose: a failed restore or update may have
    left its files half-written, an abandoned world reset its world folder moved aside. Jobs
    that never took the server (a countdown that was cancelled), jobs that never ran
    (RESUME_HARMLESS_ERROR_CODES) and the kinds in RESUME_HARMLESS_JOB_KINDS do not count.
    """
    from apps.jobs.models import Job, JobStatus

    if flagged_at is None:
        return None
    return (
        Job.objects.filter(
            instance=instance,
            exclusive=True,
            finished_at__gt=flagged_at,
            status__in=(JobStatus.FAILED, JobStatus.CANCELLED),
        )
        .exclude(kind__in=RESUME_HARMLESS_JOB_KINDS)
        .exclude(status=JobStatus.FAILED, error_code__in=RESUME_HARMLESS_ERROR_CODES)
        .order_by("-finished_at")
        .first()
    )


def _give_up(instance: Instance, result: dict[str, Any], reason: str, **details: Any) -> None:
    """Drop the mark of a server that is not started again by the panel, and say why in the audit log."""
    from apps.audit import service as audit

    if not _clear_resume(instance):
        return
    flagged_at = instance.resume_flagged_at
    audit.record(
        NOT_RESUMED_AUDIT,
        instance=instance,
        target=instance,
        details={"reason": reason, "stopped_at": flagged_at.isoformat() if flagged_at else None, **details},
    )
    log.info("resume after host shutdown: %s (%s) is not started again: %s", instance.name, instance.id, reason)
    result["cleared"].append(str(instance.id))


def resume_flagged_servers(now: datetime | None = None) -> dict[str, Any]:
    """Start servers that ``manage.py host_shutdown`` stopped before the host stopped Docker.

    Runs every minute from the scheduler tick and starts at most ONE server per run, so the
    servers come back one after the other instead of all at once. For every marked server:

    * archived: the mark is dropped;
    * no longer meant to run (``desired_state`` is not "running": something decided since the
      mark that it stays down): the mark is dropped, the server is not started;
    * marked while this panel container was already running: nothing is done. The host is
      still shutting down (the hook has returned, Docker is not stopped yet); a server started
      now would be killed seconds later. Servers are resumed by the panel that starts after
      Docker came back. A mark that still waits like this after RESUME_STALE_AFTER is dropped:
      Docker was never stopped, and the status would promise an automatic start for ever;
    * a job that held it failed, was marked as failed or was interrupted after the mark
      (``_job_that_left_it_stopped``): the mark is dropped, the server is not started;
    * its agent does not answer: nothing is done this run;
    * its container runs already (Docker's restart policy or someone started it): the mark is
      dropped;
    * a job holds it: it is skipped this run;
    * otherwise the normal ``start`` job is queued (it recreates a container that reads
      restart-required), the mark is dropped and ``instance.resumed_after_boot`` is audited.

    A mark dropped without a start is audited as ``instance.not_resumed_after_boot`` with the
    reason; the server then shows its normal status and stays down until someone starts it.
    """
    from apps.audit import service as audit
    from apps.jobs.models import ACTIVE_STATUSES, Job
    from apps.nodes.client import AgentClient

    now = now or timezone.now()
    result: dict[str, Any] = {"started": None, "cleared": [], "waiting": 0}
    marked = list(Instance.objects.filter(resume_after_boot=True).select_related("node").order_by("resume_flagged_at", "name"))
    if not marked:
        return result
    panel_started = runtime_started_at()
    containers_by_node: dict[int, dict[str, dict[str, Any]] | None] = {}
    for inst in marked:
        if inst.archived_at:
            if _clear_resume(inst):
                result["cleared"].append(str(inst.id))
            continue
        if inst.desired_state != DesiredState.RUNNING:
            _give_up(inst, result, "not_meant_to_run", desired_state=inst.desired_state)
            continue
        flagged_at = inst.resume_flagged_at
        # A mark dated in the future (clock trouble at shutdown) must not block the server forever.
        if flagged_at is not None and flagged_at <= now and panel_started <= flagged_at:
            if now - flagged_at > RESUME_STALE_AFTER:
                _give_up(inst, result, "host_not_restarted", waited_seconds=int((now - flagged_at).total_seconds()))
            else:
                result["waiting"] += 1
            continue
        failed = _job_that_left_it_stopped(inst, flagged_at)
        if failed is not None:
            _give_up(
                inst,
                result,
                "job_failed",
                job=str(failed.id),
                kind=failed.kind,
                status=failed.status,
                error_code=failed.error_code,
            )
            continue
        if inst.node_id not in containers_by_node:
            try:
                listed = AgentClient(inst.node, timeout=RESUME_AGENT_TIMEOUT).list_containers()
                containers_by_node[inst.node_id] = {str(c.get("instance_id")): c for c in listed}
            except ApiError as exc:
                log.info("resume after host shutdown: the agent of node %s does not answer yet (%s)", inst.node.name, exc)
                containers_by_node[inst.node_id] = None
        containers = containers_by_node[inst.node_id]
        if containers is None:
            result["waiting"] += 1
            continue
        if _live(containers.get(str(inst.id))):
            if _clear_resume(inst):
                result["cleared"].append(str(inst.id))
            continue
        if result["started"] is not None:
            result["waiting"] += 1  # one start per run
            continue
        if inst.active_job_id and Job.objects.filter(pk=inst.active_job_id, status__in=ACTIVE_STATUSES).exists():
            result["waiting"] += 1
            continue
        try:
            # one transaction: the mark is only dropped together with the queued start
            with transaction.atomic():
                if not _clear_resume(inst):
                    continue  # an explicit start/stop/kill settled it meanwhile
                job = enqueue("start", title="Start server (after the host restart)", instance=inst)
                audit.record(
                    "instance.resumed_after_boot",
                    instance=inst,
                    target=inst,
                    details={"job": str(job.id), "stopped_at": flagged_at.isoformat() if flagged_at else None},
                )
        except ConflictError:
            result["waiting"] += 1  # another operation took the server meanwhile: next run
            continue
        log.info("resume after host shutdown: queued the start of %s (%s)", inst.name, inst.id)
        result["started"] = str(inst.id)
    return result
