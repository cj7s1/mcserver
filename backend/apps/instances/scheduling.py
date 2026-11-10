"""Cron-style schedules (restart/backup/command/start/stop) and the restart of servers after a host shutdown."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from django.conf import settings
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


# --- cron schedules ------------------------------------------------------------------------------
# A schedule that finds its server busy (another job holds it) or without room in the node's
# memory is not dropped: it is tried again on every tick for DEFER_LIMIT, then recorded as
# failed and notified.
DEFER_LIMIT = timedelta(minutes=60)
DEFER_CODES = frozenset({"instance_busy", "insufficient_memory"})
# A power action or command whose time is further in the past than this (the panel was down) is
# not run late: a restart, stop or announcement hours after its time is worse than none. The same
# holds for one that was put off when the panel stopped and started again while it waited.
MISFIRE_GRACE = timedelta(minutes=10)
POWER_ACTIONS = frozenset({"restart", "start", "stop", "command"})
# A backup or snapshot that was missed that way runs once, but not on servers that are still
# coming up: only when the server has been ready for CATCH_UP_READY_FOR, or the panel has been
# up for CATCH_UP_PANEL_UP_FOR (a stopped server, a server that does not get ready).
CATCH_UP_READY_FOR = timedelta(minutes=2)
CATCH_UP_PANEL_UP_FOR = timedelta(minutes=5)
# Within one tick: backups first, then snapshots, then power actions and commands. On one server
# the first takes the lock and the others wait for the next ticks.
ACTION_ORDER = {"backup": 0, "snapshot": 1}
RESULT_MAX = 300  # Schedule.last_result


class Deferred(Exception):
    """Not now: the schedule is tried again on the next tick."""


def schedule_timezone() -> ZoneInfo:
    """The time zone schedules are read in: the servers' (system setting ``timezone``, the
    same TZ the server containers get), else the panel's TIME_ZONE."""
    from apps.core.models import SystemSetting

    for name in (SystemSetting.get("timezone"), settings.TIME_ZONE, "UTC"):
        if not name:
            continue
        try:
            return ZoneInfo(str(name))
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def validate_cron(expr: str) -> str:
    expr = " ".join(expr.split())
    if not croniter.is_valid(expr) or len(expr.split()) != 5:
        raise ValueError("Use a 5-field cron expression, e.g. '0 4 * * *' (04:00 daily)")
    try:
        next_run(expr, timezone.now())
    except (ValueError, KeyError, IndexError) as exc:  # croniter's errors derive from ValueError
        raise ValueError(
            "This cron expression never matches a date (e.g. day 31 of a month that has 30 days, or 30 February)"
        ) from exc
    return expr


def _exists(wall: datetime, tz: ZoneInfo) -> bool:
    """Whether this wall-clock time occurs in ``tz`` (not inside the hour skipped in spring)."""
    return wall.replace(tzinfo=tz).astimezone(UTC).astimezone(tz).replace(tzinfo=None) == wall


def _instant(wall: datetime, tz: ZoneInfo) -> datetime:
    """The moment a wall-clock time stands for. A time that occurs twice (the hour repeated in
    autumn) is its first occurrence; a time that does not occur (the hour skipped in spring) is
    the first time after the gap."""
    if _exists(wall, tz):
        return wall.replace(tzinfo=tz, fold=0)
    probe = wall.replace(second=0, microsecond=0)
    for _ in range(24 * 60):
        probe += timedelta(minutes=1)
        if _exists(probe, tz):
            return probe.replace(tzinfo=tz, fold=0)
    return wall.replace(tzinfo=tz, fold=0)


def names_fixed_hours(expr: str) -> bool:
    """Whether the expression's hour field names hours (``2``, ``1,13``, ``1-3``) rather than
    running on an interval (``*`` or a step such as ``*/2`` / ``1-23/2``)."""
    fields = expr.split()
    hour = fields[1] if len(fields) >= 2 else "*"
    return "*" not in hour and "/" not in hour


def _ambiguous(wall: datetime, tz: ZoneInfo) -> bool:
    """Whether this wall-clock time occurs twice (the hour repeated when daylight saving time ends)."""
    if not _exists(wall, tz):
        return False
    return wall.replace(tzinfo=tz, fold=0).utcoffset() != wall.replace(tzinfo=tz, fold=1).utcoffset()


def next_run(expr: str, after: datetime, tz: ZoneInfo | None = None) -> datetime:
    """The first time after ``after`` the expression matches, in the servers' time zone.

    The expression is matched against wall-clock times. A time inside the hour skipped when
    daylight saving time begins runs at the end of that gap (03:00). For the hour that is
    repeated when it ends:

    * a schedule whose hour field names hours (``30 1 * * *``: "at 01:30") runs once, at the
      first occurrence: a nightly job does not run twice that night;
    * an interval schedule (hour field ``*`` or a step: hourly, every 15 minutes, every 2 hours)
      runs on real elapsed time, so it runs in both occurrences of the hour instead of skipping
      an hour of real time.
    """
    tz = tz or schedule_timezone()
    after_utc = after.astimezone(UTC)
    both_folds = not names_fixed_hours(expr)
    # Start a little before ``after``: the second occurrence of a repeated hour has the wall time
    # of an earlier moment. Candidates are compared in UTC (aware datetimes that share a tzinfo
    # are compared by their wall time, fold ignored).
    wall = (after_utc - timedelta(hours=2)).astimezone(tz).replace(tzinfo=None)
    it = croniter(expr, wall)
    best: datetime | None = None
    for _ in range(4000):
        candidate = it.get_next(datetime)
        first = _instant(candidate, tz).astimezone(UTC)
        if best is not None and first >= best:
            break  # the first occurrences never get earlier: nothing later can beat ``best``
        moments = [first]
        if both_folds and _ambiguous(candidate, tz):
            moments.append(candidate.replace(tzinfo=tz, fold=1).astimezone(UTC))
        for moment in moments:
            if moment > after_utc and (best is None or moment < best):
                best = moment
    if best is None:
        raise ValueError(f"no time after {after.isoformat()} matches '{expr}'")
    return best.astimezone(tz)


def _result(text: str) -> str:
    return text if len(text) <= RESULT_MAX else text[: RESULT_MAX - 1] + "…"


def _local(when: datetime) -> str:
    return when.astimezone(schedule_timezone()).strftime("%Y-%m-%d %H:%M")


def run_due_schedules(now: datetime) -> int:
    """Run the schedules that are due; returns how many ran or were settled (put-off ones do not count).

    Every schedule is handled on its own: an error in one (a bad expression saved by an older
    release, an unexpected failure) is recorded on it and never keeps the others from running.
    """
    count = 0
    due: list[Schedule] = []
    for sched in Schedule.objects.filter(enabled=True).select_related("instance", "instance__node"):
        try:
            if sched.next_run_at is None:
                sched.next_run_at = next_run(sched.cron, now)
                sched.save(update_fields=["next_run_at"])
            elif sched.next_run_at <= now:
                due.append(sched)
        except Exception as exc:
            _pause_broken(sched, exc)
    due.sort(key=lambda s: (ACTION_ORDER.get(s.action, 2), s.instance.name, s.name, s.pk))
    panel_up = now - runtime_started_at()
    for sched in due:
        try:
            count += _run_one(sched, now, panel_up)
        except Exception as exc:  # keep the scheduler alive
            log.exception("schedule %s failed", sched.pk)
            try:
                Schedule.objects.filter(pk=sched.pk).update(
                    last_run_at=now,
                    next_run_at=next_run(sched.cron, now),
                    deferred_at=None,
                    last_result=_result(f"Error: {exc}"),
                )
            except Exception as again:
                _pause_broken(sched, again)
            count += 1
    return count


def _pause_broken(sched: Schedule, exc: Exception) -> None:
    """A schedule whose expression cannot be evaluated (no next date): paused, with the reason."""
    log.warning("schedule %s (%s) is paused: its time cannot be computed (%s)", sched.pk, sched.cron, exc)
    try:
        Schedule.objects.filter(pk=sched.pk).update(
            enabled=False,
            next_run_at=None,
            deferred_at=None,
            last_result=_result(f"Paused: the cron expression '{sched.cron}' never matches a date. Correct it and resume it."),
        )
    except Exception:
        log.exception("could not pause schedule %s", sched.pk)


def _run_one(sched: Schedule, now: datetime, panel_up: timedelta) -> int:
    due = sched.next_run_at
    assert due is not None
    late = now - due
    waited = now - sched.deferred_at if sched.deferred_at is not None else None
    if sched.action in POWER_ACTIONS:
        # not put off: late because the panel was not running at its time. Put off: the panel
        # stopped (and started again) while the run waited, and the wait is past the grace too
        missed = late > MISFIRE_GRACE if waited is None else (waited > MISFIRE_GRACE and panel_up < waited)
        if missed:
            return _missed(sched, now, due)
    if waited is not None and waited >= DEFER_LIMIT:
        # the wait ran out (the panel may have been down meanwhile): give up without running it late
        return _give_up_deferred(sched, now, _waiting_reason(sched) or "it was put off and its waiting time ran out")
    try:
        if sched.deferred_at is not None or late > MISFIRE_GRACE:
            _settled_enough(sched, panel_up, now)
        result = execute(sched)
    except Deferred as wait:
        return _defer(sched, now, str(wait))
    except ConflictError as exc:
        if exc.error_code not in DEFER_CODES:
            return _done(sched, now, f"Skipped: {exc.detail}")
        return _defer(sched, now, str(exc.detail))
    except ApiError as exc:
        return _done(sched, now, f"Skipped: {exc.detail}")
    return _done(sched, now, result)


def _settled_enough(sched: Schedule, panel_up: timedelta, now: datetime) -> None:
    """A missed (or put-off) backup or snapshot waits until its server has been ready for a
    while, or the panel has been up long enough: not on a server that is just starting."""
    if sched.action in POWER_ACTIONS or panel_up >= CATCH_UP_PANEL_UP_FOR:
        return
    inst = sched.instance
    if inst.status == "running" and inst.ready_at is not None and now - inst.ready_at >= CATCH_UP_READY_FOR:
        return
    raise Deferred("catching up a run that was missed while the panel was not running, once the server has settled")


def _missed(sched: Schedule, now: datetime, due: datetime) -> int:
    sched.next_run_at = next_run(sched.cron, now)
    sched.deferred_at = None
    sched.last_result = _result(
        f"Missed: due at {_local(due)} while the panel was not running; not run late (next: {_local(sched.next_run_at)})"
    )
    sched.save(update_fields=["next_run_at", "deferred_at", "last_result"])
    log.info("schedule %s (%s) missed its time %s; not run late", sched.pk, sched.name, due.isoformat())
    return 1


def _waiting_reason(sched: Schedule) -> str:
    """The reason of the last "Waiting: <why> (due at ...)" result, if that is what it says."""
    text = sched.last_result or ""
    if not text.startswith("Waiting: "):
        return ""
    text = text[len("Waiting: ") :]
    # (a long reason was cut to the length of last_result)
    return text.rsplit(" (due at ", 1)[0].strip() if " (due at " in text else text.rstrip("…").strip()


def _done(sched: Schedule, now: datetime, result: str) -> int:
    sched.last_run_at = now
    sched.next_run_at = next_run(sched.cron, now)
    sched.deferred_at = None
    sched.last_result = _result(result)
    sched.save(update_fields=["last_run_at", "next_run_at", "deferred_at", "last_result"])
    return 1


def _defer(sched: Schedule, now: datetime, why: str) -> int:
    since = sched.deferred_at or now
    if now - since < DEFER_LIMIT:
        sched.deferred_at = since
        sched.last_result = _result(
            f"Waiting: {why} (due at {_local(sched.next_run_at or now)}; tried again every minute until {_local(since + DEFER_LIMIT)})"
        )
        sched.save(update_fields=["deferred_at", "last_result"])
        return 0
    return _give_up_deferred(sched, now, why)


def _give_up_deferred(sched: Schedule, now: datetime, why: str) -> int:
    due = sched.next_run_at or sched.deferred_at or now
    sched.next_run_at = next_run(sched.cron, now)
    sched.deferred_at = None
    sched.last_result = _result(f"Failed: not run for {int(DEFER_LIMIT.total_seconds() // 60)} minutes after its time: {why}")
    sched.save(update_fields=["next_run_at", "deferred_at", "last_result"])
    log.warning("schedule %s (%s) of %s was not run: %s", sched.pk, sched.name, sched.instance.name, why)
    _notify_not_run(sched, due, why)
    return 1


def _notify_not_run(sched: Schedule, due: datetime, why: str) -> None:
    from apps.notifications.service import notify

    inst = sched.instance
    title = f"Schedule '{sched.name}' did not run on {inst.name}"
    message = (
        f"The {sched.action} due at {_local(due)} was put off for {int(DEFER_LIMIT.total_seconds() // 60)} minutes "
        f"and then given up: {why}. It runs again at its next time ({_local(sched.next_run_at or due)})."
    )
    data = {"schedule": sched.name, "action": sched.action, "due_at": due.isoformat(), "reason": why[:500]}
    notify("job.failed", title, message, instance=inst, data=data)
    if sched.action == "backup":
        notify("backup.failed", title, message, instance=inst, data=data)


def execute(sched: Schedule) -> str:
    inst: Instance = sched.instance
    if inst.archived_at:
        return "Skipped: server archived"
    # whether the server process runs (also while a backup job makes its status "backing up")
    running = inst.container_state == "running" or inst.status in ("running", "starting", "unhealthy")
    if sched.only_when_running and sched.action in ("restart", "command", "stop") and not running:
        return "Skipped: server not running"
    if sched.action in ("restart", "start", "stop"):
        from . import capacity
        from .power_warnings import clean_message
        from .services import enqueue_power

        if sched.action in ("start", "restart"):
            short = capacity.insufficient(inst)
            if short is not None:
                raise ConflictError(short.message(inst.name), code="insufficient_memory", details=short.details())
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


def _give_up(instance: Instance, result: dict[str, Any], reason: str, **details: Any) -> bool:
    """Drop the mark of a server that is not started again by the panel, and say why in the audit
    log. False when the mark was settled meanwhile (nothing recorded)."""
    from apps.audit import service as audit

    if not _clear_resume(instance):
        return False
    flagged_at = instance.resume_flagged_at
    audit.record(
        NOT_RESUMED_AUDIT,
        instance=instance,
        target=instance,
        details={"reason": reason, "stopped_at": flagged_at.isoformat() if flagged_at else None, **details},
    )
    log.info("resume after host shutdown: %s (%s) is not started again: %s", instance.name, instance.id, reason)
    result["cleared"].append(str(instance.id))
    return True


def _notify_not_resumed(instance: Instance, why: str) -> None:
    """A server that ran before the host shutdown stays down for lack of memory: notified, not only audited."""
    from apps.notifications.service import notify

    notify(
        "job.failed",
        f"{instance.name} was not started again after the host restart",
        f"{why} Stop another server or lower a memory limit, then start it.",
        instance=instance,
        data={"reason": "insufficient_memory"},
    )


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
    * it does not fit into the node's memory next to the servers running there (``capacity``):
      it is skipped this run, and the mark is dropped (audited with the numbers) once the panel
      has been up for RESUME_STALE_AFTER;
    * otherwise the normal ``start`` job is queued (it recreates a container that reads
      restart-required), the mark is dropped and ``instance.resumed_after_boot`` is audited.

    A mark dropped without a start is audited as ``instance.not_resumed_after_boot`` with the
    reason; the server then shows its normal status and stays down until someone starts it.
    """
    from apps.audit import service as audit
    from apps.jobs.models import ACTIVE_STATUSES, Job
    from apps.nodes.client import AgentClient

    from . import capacity

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
        short = capacity.insufficient(inst, client=AgentClient(inst.node, timeout=RESUME_AGENT_TIMEOUT))
        if short is not None:
            if now - panel_started > RESUME_STALE_AFTER:
                if _give_up(inst, result, "insufficient_memory", **short.details()):
                    _notify_not_resumed(inst, short.message(inst.name))
            else:
                log.info("resume after host shutdown: %s waits: %s", inst.name, short.message(inst.name))
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
