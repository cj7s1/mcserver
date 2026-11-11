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
    for inst in qs.only("id", "name", "game_port", "extra_ports", "ports"):
        used[inst.game_port] = f"server '{inst.name}'"
        for extra in inst.extra_ports or []:
            try:
                used[int(extra["host_port"])] = f"server '{inst.name}' ({extra.get('label') or 'extra port'})"
            except (KeyError, TypeError, ValueError):
                continue
        # named ports of other games: a Minecraft server never takes their numbers, whatever the protocol
        for named in inst.ports or []:
            try:
                used.setdefault(int(named["host_port"]), f"server '{inst.name}' ({named.get('name') or 'port'})")
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


# --- named ports of games other than Minecraft (protocol-aware) -----------------------------------------
NAMED_PORT_SEARCH = 100  # how far above a game's well-known port a free one is looked for


def ports_in_use_by_protocol(
    node: Node, *, exclude: Instance | None = None, include_agent: bool = True
) -> dict[tuple[int, str], str]:
    """Map of (host port, protocol) -> what uses it: panel servers (Minecraft's game port is TCP, its
    extra ports have their own protocol; other games list named ports) and the Docker host. TCP 7777
    and UDP 7777 are different ports."""
    used: dict[tuple[int, str], str] = {}
    qs = Instance.objects.filter(node=node)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    for inst in qs.only("id", "name", "game", "game_port", "extra_ports", "ports"):
        named = [p for p in inst.ports or [] if isinstance(p, dict)]
        if not named:
            used[(int(inst.game_port), "tcp")] = f"server '{inst.name}'"
        for extra in inst.extra_ports or []:
            try:
                key = (int(extra["host_port"]), str(extra.get("protocol") or "tcp"))
            except (KeyError, TypeError, ValueError):
                continue
            used[key] = f"server '{inst.name}' ({extra.get('label') or 'extra port'})"
        for port in named:
            try:
                key = (int(port["host_port"]), str(port.get("protocol") or "tcp"))
            except (KeyError, TypeError, ValueError):
                continue
            used[key] = f"server '{inst.name}' ({port.get('name') or 'port'})"
    if include_agent:
        try:
            for p in AgentClient(node).published_ports():
                if exclude is not None and p.get("instance_id") == str(exclude.pk):
                    continue
                key = (int(p["host_port"]), str(p.get("protocol") or "tcp"))
                used.setdefault(key, f"Docker container '{p.get('container')}'")
        except ApiError as exc:
            log.warning("Could not query agent for published ports: %s", exc)
    return used


def assign_named_ports(
    definition: Any,
    node: Node,
    requested: dict[str, Any] | None = None,
    *,
    host_ip: str = "",
    exclude: Instance | None = None,
    current: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The named ports of a server of ``definition``: ``[{name, container_port, protocol, host_port,
    host_ip}]`` in the definition's order.

    A requested host port (``requested[name]``, or an int under ``host_port`` of a dict) must be free
    for its protocol (409 ``port_conflict``). A port not requested keeps its ``current`` host port, or
    gets the game's well-known port when that is free, else the next free one above it, else the
    first free one of the panel's range (409 ``no_free_ports``)."""
    requested = dict(requested or {})
    known = {p.name for p in definition.ports}
    if unknown := sorted(set(requested) - known):
        raise ApiError(f"Unknown port(s) for {definition.label}: {', '.join(unknown)}", code="invalid_port")
    previous = {str(p.get("name")): p for p in current or [] if isinstance(p, dict)}
    used = ports_in_use_by_protocol(node, exclude=exclude)
    taken: set[tuple[int, str]] = set()
    start, end = port_range()
    out: list[dict[str, Any]] = []
    for spec in definition.ports:
        if getattr(spec, "same_port_as", "") or getattr(spec, "offset_of", ""):
            out.append(_same_port(definition, spec, out, requested.get(spec.name), used, taken, previous.get(spec.name)))
            continue
        # a port whose number another port shares (Satisfactory's 7777 for UDP and TCP) or derives from
        # (Valheim's query port = game port + 1): free for all of them
        group = [(0, spec.protocol)] + [
            (int(getattr(s, "offset", 0) or 0) if getattr(s, "offset_of", "") else 0, s.protocol)
            for s in definition.ports
            if spec.name in (getattr(s, "same_port_as", ""), getattr(s, "offset_of", ""))
        ]
        want = requested.get(spec.name)
        ip = host_ip
        if isinstance(want, dict):
            ip = str(want.get("host_ip") or host_ip or "")
            want = want.get("host_port")
        if want in (None, "", 0):
            if spec.name in previous:
                want = int(previous[spec.name]["host_port"])
                ip = ip or str(previous[spec.name].get("host_ip") or "")
                if (want, spec.protocol) in taken:
                    want = None
        if want not in (None, "", 0):
            try:
                port = int(want)
            except (TypeError, ValueError):
                raise ApiError(f"{spec.label}: the host port must be a number", code="invalid_port") from None
            if not 1024 <= port <= 65535:
                raise ApiError("Ports must be between 1024 and 65535", code="invalid_port")
            if port in getattr(spec, "avoid", ()):
                raise ApiError(f"{spec.label} cannot use port {port}", code="invalid_port", details={"port_name": spec.name})
            key = (port, spec.protocol)
            if key in used or key in taken:
                what = used.get(key) or f"its own {spec.protocol.upper()} port"
                raise ConflictError(
                    f"{spec.protocol.upper()} port {port} is already used by {what}",
                    code="port_conflict",
                    details={"port": port, "protocol": spec.protocol, "name": spec.name, "port_name": spec.name},
                )
        else:
            candidates = [*range(spec.default_host_port, min(65535, spec.default_host_port + NAMED_PORT_SEARCH) + 1)]
            candidates += [p for p in range(start, end + 1) if p not in candidates]
            avoid = getattr(spec, "avoid", frozenset())
            port = next(
                (
                    p
                    for p in candidates
                    if p not in avoid
                    and all(
                        1024 <= p + off <= 65535 and (p + off, proto) not in used and (p + off, proto) not in taken
                        for off, proto in group
                    )
                ),
                0,
            )
            if not port:
                raise ConflictError(
                    f"No free {spec.protocol.upper()} port for {spec.label}",
                    code="no_free_ports",
                    details={"port_name": spec.name},
                )
        taken.add((port, spec.protocol))
        entry = {"name": spec.name, "container_port": _container_port(spec, port), "protocol": spec.protocol, "host_port": port}
        if ip:
            ipaddress_ok(ip)
            entry["host_ip"] = ip
        out.append(entry)
    return out


def _container_port(spec: Any, host_port: int) -> int:
    """The port the server listens on inside its container: the host port's number for a port published 1:1
    (``PortSpec.one_to_one``: the image takes it from a variable), else the definition's container port."""
    return int(host_port) if getattr(spec, "one_to_one", False) else int(spec.container_port)


def _same_port(
    definition: Any,
    spec: Any,
    out: list[dict[str, Any]],
    want: Any,
    used: dict[tuple[int, str], str],
    taken: set[tuple[int, str]],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The entry of a port that always has the host port of another one (``PortSpec.same_port_as``)
    or that number plus an offset (``PortSpec.offset_of``, Valheim's query port). A requested number
    other than that is refused, unless it is the port's current number (an editor that sends every
    port unchanged)."""
    leader_name = getattr(spec, "same_port_as", "") or spec.offset_of
    offset = 0 if getattr(spec, "same_port_as", "") else int(getattr(spec, "offset", 0) or 0)
    leader = next((e for e in out if e["name"] == leader_name), None)
    if leader is None:
        raise ValueError(f"{definition.id}: port {spec.name} must come after {leader_name}")
    port = int(leader["host_port"]) + offset
    if isinstance(want, dict):
        want = want.get("host_port")
    if want not in (None, "", 0) and str(want) not in (str(port), str((previous or {}).get("host_port"))):
        if offset:
            raise ApiError(
                f"{spec.label} always uses the game port + {offset} ({port}); change the game port instead",
                code="invalid_port",
            )
        raise ApiError(f"{spec.label} always uses the same port number as the game port ({port})", code="invalid_port")
    if not 1024 <= port <= 65535:
        raise ApiError(f"{spec.label} would be port {port}; choose a lower game port", code="invalid_port")
    key = (port, spec.protocol)
    if key in used or key in taken:
        raise ConflictError(
            f"{spec.protocol.upper()} port {port} is already used by {used.get(key) or 'this server'}; "
            f"{definition.label} needs it for {spec.label} too",
            code="port_conflict",
            details={"port": port, "protocol": spec.protocol, "name": spec.name, "port_name": leader_name},
        )
    taken.add(key)
    entry = {"name": spec.name, "container_port": _container_port(spec, port), "protocol": spec.protocol, "host_port": port}
    if leader.get("host_ip"):
        entry["host_ip"] = leader["host_ip"]
    return entry


def ipaddress_ok(value: str) -> str:
    import ipaddress

    try:
        ipaddress.ip_address(value)
    except ValueError:
        raise ApiError(f"{value!r} is not an IP address", code="invalid_port") from None
    return value


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
    not_ready: bool = False,
) -> Job:
    """Queue start/stop/restart. With ``warn_minutes`` players are warned in game first.
    ``not_ready`` (a stop): the server never became ready in this run, the agent stops it at once.

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
        if not_ready:
            params["not_ready"] = True
        return enqueue(
            action,
            title=f"{base_title} ({warn} min warning)",
            instance=instance,
            user=user,
            params=params,
            exclusive=False,
        )
    immediate: dict[str, Any] = {"allow_overcommit": True} if allow_overcommit and action != "stop" else {}
    if not_ready and action == "stop":
        immediate["not_ready"] = True
    job = enqueue(action, title=base_title, instance=instance, user=user, params=immediate or None)
    if countdown is not None:
        try:
            cancel_countdown(countdown.id, message=f"Superseded by '{base_title}'", notify_players=False)
        except ConflictError:
            pass  # it finished or entered its final seconds meanwhile
    return job


def end_first_start_wait(instance: Instance, user: Any) -> Any:
    """End the server's job when all it does is wait for a start that downloads the game (CS2, Rust, Project
    Zomboid: ``game_handlers.wait_first_start``, flagged in ``Job.result``), so that a Stop is not refused while
    that wait lasts (up to hours). The job is cancelled under its row lock, so one that has gone on to other work
    meanwhile is left alone. Returns the ended job, else None."""
    from apps.jobs.models import Job, JobStatus
    from apps.jobs.runner import finish

    from .game_handlers import WAITING_FIRST_START

    if not instance.active_job_id:
        return None
    with transaction.atomic():
        job = Job.objects.select_for_update().filter(pk=instance.active_job_id, status=JobStatus.RUNNING).first()
        if job is None or not (job.result or {}).get(WAITING_FIRST_START):
            return None
        who = getattr(user, "username", None) or "the panel"
        if not finish(
            job,
            JobStatus.CANCELLED,
            error=f"Stopped by {who} while the server was still starting (the image downloads the game); "
            "the server was stopped instead of waiting for it",
            error_code="superseded",
        ):
            return None
    instance.refresh_from_db(fields=["active_job"])
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
    superseded = end_first_start_wait(instance, user) if action == "stop" else None
    if superseded is not None:
        audit.record(
            "job.superseded",
            request=request,
            instance=instance,
            target_type="job",
            target_id=str(superseded.id),
            details={"kind": superseded.kind, "by": "stop"},
        )
    job = enqueue_power(
        instance,
        action,
        user=user,
        warn_minutes=warn_minutes,
        warn_message=warn_message,
        allow_overcommit=allow_overcommit,
        # the ended job still waited for the first start: the server has nothing to save yet (BUG-9)
        not_ready=superseded is not None,
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
