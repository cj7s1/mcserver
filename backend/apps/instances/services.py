"""Instance domain services: port allocation, creation, power actions."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Any

from croniter import croniter
from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, IntegerField, Max, OuterRef, Subquery
from django.db.models.functions import Coalesce

from apps.audit import service as audit
from apps.core.errors import ApiError, ConflictError
from apps.core.models import SystemSetting
from apps.jobs.models import Job
from apps.jobs.runner import enqueue
from apps.nodes.client import AgentClient
from apps.nodes.models import Node

from .models import Instance, Schedule

log = logging.getLogger(__name__)


def port_range() -> tuple[int, int]:
    start = SystemSetting.get("port_range_start") or settings.MCPANEL_PORT_RANGE_START
    end = SystemSetting.get("port_range_end") or settings.MCPANEL_PORT_RANGE_END
    return int(start), int(end)


def ports_in_use(node: Node, *, exclude: Instance | None = None, include_agent: bool = True) -> dict[int, str]:
    """Map of host port -> description of what uses it (panel instances + Docker host)."""
    used: dict[int, str] = {}
    qs = Instance.objects.filter(node=node)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    for inst in qs.only("id", "name", "game_port", "extra_ports"):
        used[inst.game_port] = f"server '{inst.name}'"
        for extra in inst.extra_ports or []:
            try:
                used[int(extra["host_port"])] = f"server '{inst.name}' ({extra.get('label') or 'extra port'})"
            except (KeyError, TypeError, ValueError):
                continue
    if include_agent:
        try:
            for p in AgentClient(node).published_ports():
                if exclude is not None and p.get("instance_id") == str(exclude.pk):
                    continue
                used.setdefault(int(p["host_port"]), f"Docker container '{p.get('container')}'")
        except ApiError as exc:
            log.warning("Could not query agent for published ports: %s", exc)
    return used


def check_port_available(node: Node, port: int, *, exclude: Instance | None = None) -> None:
    if not 1024 <= port <= 65535:
        raise ApiError("Ports must be between 1024 and 65535", code="invalid_port")
    used = ports_in_use(node, exclude=exclude)
    if port in used:
        raise ConflictError(f"Port {port} is already used by {used[port]}", code="port_conflict", details={"port": port})


def allocate_port(node: Node, *, exclude: Instance | None = None) -> int:
    start, end = port_range()
    used = ports_in_use(node, exclude=exclude)
    for port in range(start, end + 1):
        if port not in used:
            return port
    raise ConflictError(f"No free ports left in the range {start}-{end}", code="no_free_ports")


def bump_revision(instance: Instance) -> None:
    Instance.objects.filter(pk=instance.pk).update(config_revision=F("config_revision") + 1)
    instance.refresh_from_db(fields=["config_revision"])


POWER_TITLES = {"start": "Start server", "stop": "Stop server", "restart": "Restart server"}


def enqueue_power(
    instance: Instance,
    action: str,
    *,
    user: Any = None,
    warn_minutes: int = 0,
    warn_message: str = "",
    title: str | None = None,
    allow_overcommit: bool = False,
) -> Job:
    """Queue start/stop/restart. With ``warn_minutes`` players are warned in game first.

    A warning job is enqueued non-exclusive and only takes the instance lock for its last
    seconds (see power_warnings). An immediate stop/restart supersedes a pending countdown.
    ``allow_overcommit``: the job starts the server even when it does not fit into the node's
    memory (``capacity``).
    """
    from apps.jobs.models import ACTIVE_STATUSES

    from .power_warnings import WARNABLE_ACTIONS, cancel_countdown, pending_countdown

    if action not in POWER_TITLES:
        raise ApiError(f"Unknown power action {action!r}", code="invalid_action")
    base_title = title or POWER_TITLES[action]
    warn = warn_minutes if action in WARNABLE_ACTIONS else 0
    countdown = pending_countdown(instance) if action in WARNABLE_ACTIONS else None
    if warn and instance.status in ("running", "starting", "unhealthy"):
        if countdown is not None:
            raise ConflictError(
                f"A countdown is already running for this server ({countdown.title}). Cancel it first.",
                code="countdown_pending",
                details={"job_id": str(countdown.id), "kind": countdown.kind},
            )
        current = Job.objects.filter(pk=instance.active_job_id, status__in=ACTIVE_STATUSES).first()
        if current is not None:  # same rule as an immediate action; the lock is taken later
            raise ConflictError(
                f"Another operation is in progress: {current.title}",
                code="instance_busy",
                details={"job_id": str(current.id), "kind": current.kind},
            )
        params: dict[str, Any] = {"warn_minutes": warn, "warn_message": warn_message}
        if allow_overcommit:
            params["allow_overcommit"] = True
        return enqueue(
            action,
            title=f"{base_title} ({warn} min warning)",
            instance=instance,
            user=user,
            params=params,
            exclusive=False,
        )
    job = enqueue(
        action,
        title=base_title,
        instance=instance,
        user=user,
        params={"allow_overcommit": True} if allow_overcommit and action != "stop" else None,
    )
    if countdown is not None:
        try:
            cancel_countdown(countdown.id, message=f"Superseded by '{base_title}'", notify_players=False)
        except ConflictError:
            pass  # it finished or entered its final seconds meanwhile
    return job


def power(
    instance: Instance,
    action: str,
    user: Any,
    request: Any = None,
    *,
    warn_minutes: int = 0,
    warn_message: str = "",
    allow_overcommit: bool = False,
) -> Job | dict[str, Any]:
    """A power action a user asked for. A start or restart that does not fit into the node's
    memory is refused (409 ``insufficient_memory``, with the numbers in ``details``) unless
    ``allow_overcommit``; a stop or kill never is."""
    if instance.archived_at:
        raise ConflictError("Unarchive this server before starting it", code="archived")
    if action == "kill":
        # a kill also settles a pending "start again after the host restart" mark (host_shutdown)
        Instance.objects.filter(pk=instance.pk).update(desired_state="stopped", resume_after_boot=False, resume_flagged_at=None)
        status = AgentClient(instance.node).kill(instance.id)
        audit.record("instance.kill", request=request, instance=instance, target=instance)
        from .reconcile import reconcile_instance

        reconcile_instance(instance.id)
        return status
    allow_overcommit = bool(allow_overcommit) and action in ("start", "restart")
    if action in ("start", "restart") and not allow_overcommit:
        from . import capacity

        capacity.admit(instance)
    job = enqueue_power(
        instance,
        action,
        user=user,
        warn_minutes=warn_minutes,
        warn_message=warn_message,
        allow_overcommit=allow_overcommit,
    )
    details: dict[str, Any] = {"job": str(job.id)}
    if allow_overcommit:
        details["allow_overcommit"] = True
    if job.params.get("warn_minutes"):
        details["warn_minutes"] = job.params["warn_minutes"]
        if job.params.get("warn_message"):
            details["warn_message"] = job.params["warn_message"]
    audit.record(f"instance.{action}", request=request, instance=instance, target=instance, details=details)
    return job


@transaction.atomic
def create_instance_record(**fields: Any) -> Instance:
    return Instance.objects.create(**fields)


# --- backup overview (InstanceSerializer.last_backup_at / backup_schedules) -----------------------
def backup_overview_annotations() -> dict[str, Any]:
    """Queryset annotations with the names ``InstanceSerializer`` reads, so a list costs no query per server."""
    from apps.backups.models import Backup, BackupStatus

    newest = (
        Backup.objects.filter(instance=OuterRef("pk"), status=BackupStatus.SUCCEEDED).order_by("-created_at").values("created_at")
    )
    # order_by(): the model's default ordering would otherwise join the GROUP BY
    schedules = (
        Schedule.objects.filter(instance=OuterRef("pk"), enabled=True, action="backup")
        .order_by()
        .values("instance")
        .annotate(n=Count("pk"))
        .values("n")
    )
    return {
        "last_backup_at": Subquery(newest[:1]),
        "backup_schedules": Coalesce(Subquery(schedules, output_field=IntegerField()), 0),
    }


def last_backup_at(instance: Instance) -> datetime | None:
    """When the newest successful backup of this server (any kind) was taken."""
    from apps.backups.models import Backup, BackupStatus

    return Backup.objects.filter(instance=instance, status=BackupStatus.SUCCEEDED).aggregate(at=Max("created_at"))["at"]


def backup_schedule_count(instance: Instance) -> int:
    """Enabled schedules of this server whose action is a backup (snapshot schedules do not count)."""
    return Schedule.objects.filter(instance=instance, enabled=True, action="backup").count()


# --- default nightly backup schedule ----------------------------------------------------------------
# The same schedule the "Nightly backup" template of the Schedules page creates (frontend
# pages/instance/Schedules.tsx TEMPLATES): a backup action with an empty payload, so its backups
# are pruned by the retention policy of the Backups page. Only the time differs: the template runs
# at 04:00 for every server, this one at a minute taken from the server's id.
NIGHTLY_BACKUP_NAME = "Nightly backup"
# Daily between 03:00 and 04:59 panel time. Two schedules of one server that are due in the same
# minute do not both run (the second finds the server busy and is skipped), so minute 0 (every cron
# preset of the schedule dialog, e.g. an hourly snapshot) and the other multiples of five that
# people type by hand are never used.
NIGHTLY_BACKUP_SLOTS: tuple[tuple[int, int], ...] = tuple((hour, minute) for hour in (3, 4) for minute in range(60) if minute % 5)


def cron_minutes(expr: str) -> set[int]:
    """Minutes of the hour at which a cron expression can fire (all sixty for ``*``)."""
    try:
        minutes = croniter(expr).expanded[0]
    except (ValueError, KeyError, IndexError):  # croniter's errors derive from ValueError
        return set()
    if "*" in minutes:
        return set(range(60))
    return {int(m) for m in minutes}


def schedule_minutes(instance: Instance) -> set[int]:
    """Minutes of the hour used by any schedule of this server, paused ones included (they can be resumed)."""
    used: set[int] = set()
    for cron in Schedule.objects.filter(instance=instance).values_list("cron", flat=True):
        used |= cron_minutes(cron)
    return used


def nightly_backup_cron(instance_id: Any, taken: Collection[int] = ()) -> str:
    """Cron of the nightly backup of a server: a fixed slot derived from its id, so servers spread
    over the two hours instead of all starting at once.

    ``taken`` are minutes other schedules of the server use: the next free slot is chosen then.
    When no slot is free (a schedule that runs every minute) the server's own slot is kept.
    """
    seed = instance_id.int if isinstance(instance_id, uuid.UUID) else uuid.UUID(str(instance_id)).int
    start = seed % len(NIGHTLY_BACKUP_SLOTS)
    ordered = NIGHTLY_BACKUP_SLOTS[start:] + NIGHTLY_BACKUP_SLOTS[:start]
    hour, minute = next((slot for slot in ordered if slot[1] not in taken), ordered[0])
    return f"{minute} {hour} * * *"


def add_nightly_backup_schedule(instance: Instance) -> Schedule:
    """Create the default nightly backup schedule of a server, away from the minutes of its other schedules.

    The caller checks permissions and writes the audit event. ``next_run_at`` stays empty like for a
    schedule made through the API: the scheduler fills it in on its next tick.
    """
    return Schedule.objects.create(
        instance=instance,
        name=NIGHTLY_BACKUP_NAME,
        action="backup",
        cron=nightly_backup_cron(instance.pk, schedule_minutes(instance)),
        payload={},
        enabled=True,
        # also while stopped: an imported or edited server must not stay without any backup, and an
        # unchanged folder costs restic almost nothing
        only_when_running=False,
    )
