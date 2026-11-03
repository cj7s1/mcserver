"""Instance domain services: port allocation, creation, power actions."""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import F

from apps.audit import service as audit
from apps.core.errors import ApiError, ConflictError
from apps.core.models import SystemSetting
from apps.jobs.models import Job
from apps.jobs.runner import enqueue
from apps.nodes.client import AgentClient
from apps.nodes.models import Node

from .models import Instance

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


def power(instance: Instance, action: str, user: Any, request: Any = None) -> Job | dict[str, Any]:
    if instance.archived_at:
        raise ConflictError("Unarchive this server before starting it", code="archived")
    titles = {"start": "Start server", "stop": "Stop server", "restart": "Restart server"}
    if action == "kill":
        Instance.objects.filter(pk=instance.pk).update(desired_state="stopped")
        status = AgentClient(instance.node).kill(instance.id)
        audit.record("instance.kill", request=request, instance=instance, target=instance)
        from .reconcile import reconcile_instance

        reconcile_instance(instance.id)
        return status
    if action not in titles:
        raise ApiError(f"Unknown power action {action!r}", code="invalid_action")
    job = enqueue(action, title=titles[action], instance=instance, user=user)
    audit.record(f"instance.{action}", request=request, instance=instance, target=instance, details={"job": str(job.id)})
    return job


@transaction.atomic
def create_instance_record(**fields: Any) -> Instance:
    return Instance.objects.create(**fields)
