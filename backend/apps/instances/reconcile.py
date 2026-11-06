"""Reconcile observed container state into the database (runs every few seconds)."""

from __future__ import annotations

import logging
from typing import Any

from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.console.events import broadcast
from apps.core.errors import ApiError
from apps.jobs.models import ACTIVE_STATUSES, Job
from apps.nodes.client import AgentClient
from apps.nodes.models import Node

from . import state
from .models import Instance, Status

log = logging.getLogger(__name__)


def instance_payload(instance: Instance) -> dict[str, Any]:
    return {
        "id": str(instance.id),
        "status": instance.status,
        "status_detail": instance.status_detail,
        "container_state": instance.container_state,
        "health": instance.health,
        "started_at": instance.started_at.isoformat() if instance.started_at else None,
        "ready_at": instance.ready_at.isoformat() if instance.ready_at else None,
        "active_job_id": str(instance.active_job_id) if instance.active_job_id else None,
    }


def _ts(value: str | None):  # type: ignore[no-untyped-def]
    if not value:
        return None
    try:
        return parse_datetime(value.replace("Z", "+00:00")[:32])
    except ValueError:
        return None


CRASH_LOOP_AUTOSTOP = 5
# Prefix of the status detail set by the crash-loop auto-stop; kept until the user acts.
AUTO_STOP_PREFIX = "Stopped automatically after"
# At most one "instance.failed" notification per server in this window (seconds): a crash loop
# or a flapping server must not flood the webhook.
FAILED_NOTIFY_INTERVAL = 15 * 60
# Columns owned by the reconciler. Jobs write other columns (container_spec_hash,
# installed_at, node, ...) concurrently, so only these are written back from an observation.
OBSERVED_FIELDS = (
    "status",
    "status_detail",
    "container_state",
    "health",
    "last_exit_code",
    "oom_killed",
    "started_at",
    "ready_at",
    "status_updated_at",
)


def crash_restarts(instance: Instance, container: dict[str, Any] | None) -> int:
    """Docker restarts since the server was last healthy (baseline kept in the cache).

    Docker's RestartCount is cumulative for a container, so we remember the count at the
    start of each not-ready streak and report the difference.
    """
    c = container or {}
    key = f"crashloop:{instance.pk}"
    count = int(c.get("restart_count") or 0)
    if not c.get("exists") or c.get("health") == "healthy" or c.get("status") in ("exited", "created", "dead"):
        cache.delete(key)
        return 0
    baseline = cache.get(key)
    if baseline is None or baseline > count:  # new streak, or container recreated (count reset)
        cache.set(key, count, 24 * 3600)
        return 0
    return count - int(baseline)


def policy_restarts(instance: Instance, container: dict[str, Any] | None) -> int:
    """Restarts done by Docker's restart policy since the previous observation.

    Docker's RestartCount grows only when the restart policy brings an exited server back
    (a crash, or a stop from inside the game), never for restarts the panel performs; a manual
    start resets it and a recreated container starts at 0, so the baseline is kept per
    container id (in the cache) and only increases count.
    """
    c = container or {}
    key = f"policyrestarts:{instance.pk}"
    if not c.get("exists") or not c.get("id"):
        cache.delete(key)
        return 0
    current = (str(c["id"]), int(c.get("restart_count") or 0))
    previous = cache.get(key)
    previous = tuple(previous) if previous else None
    if previous != current:
        cache.set(key, current, 7 * 24 * 3600)
    if previous is None or previous[0] != current[0] or current[1] <= previous[1]:
        return 0
    return current[1] - previous[1]


def _notify_failed(instance: Instance, title: str, message: str, data: dict[str, Any]) -> bool:
    """Send "instance.failed", at most once per server per FAILED_NOTIFY_INTERVAL."""
    if not cache.add(f"notify:instance.failed:{instance.pk}", 1, FAILED_NOTIFY_INTERVAL):
        return False
    from apps.notifications.service import notify

    notify("instance.failed", title, message, instance=instance, data=data)
    return True


def _notify_unexpected_restart(instance: Instance, restarts: int) -> None:
    times = "" if restarts == 1 else f" ({restarts} times)"
    _notify_failed(
        instance,
        f"{instance.name} restarted unexpectedly",
        f"The server process exited without being stopped from the panel and Docker restarted it{times}. "
        "Unless this was an in-game /stop or /restart, check the console log for the crash.",
        {"status": "restarted", "detail": instance.status_detail},
    )


def _stop_crash_loop(instance: Instance, restarts: int) -> None:
    """Stop a container that keeps crashing so it does not burn CPU forever."""
    from apps.audit import service as audit

    summary = ""
    try:
        from .diagnostics import diagnose

        issues = [i for i in diagnose(instance)["issues"] if i["code"] != "crash_loop"]
        if issues:
            summary = f" Likely cause: {issues[0]['title']}."
    except Exception as exc:  # diagnostics are best effort
        log.debug("diagnostics failed: %s", exc)
    try:
        AgentClient(instance.node).stop(instance.id, timeout=15)
    except ApiError as exc:
        log.warning("could not stop crash-looping %s: %s", instance.pk, exc)
        return
    Instance.objects.filter(pk=instance.pk).update(desired_state="stopped")
    instance.desired_state = "stopped"
    instance.status = Status.FAILED
    instance.status_detail = (f"{AUTO_STOP_PREFIX} {restarts} crashes during startup.{summary}")[:500]
    audit.record("instance.crash_loop_stopped", instance=instance, target=instance, success=False, details={"restarts": restarts})
    from apps.notifications.service import notify

    notify(
        "instance.crash_loop_stopped",
        f"{instance.name} stopped after repeated crashes",
        instance.status_detail,
        instance=instance,
        data={"status": "failed", "detail": instance.status_detail},
    )


def apply_observation(
    instance: Instance, container: dict[str, Any] | None, *, agent_ok: bool = True, agent_error: str = ""
) -> bool:
    """Update an instance from an agent container description. Returns True if changed.

    Only the reconciler's own columns (OBSERVED_FIELDS) are written, and only while the
    instance still is on the node and in the archive state the observation was made for, so
    a slow reconcile never writes stale values over what a job changed meanwhile.
    installed_at and container_spec_hash are only ever filled in when still empty.
    """
    job_kind = None
    if instance.active_job_id:
        job = Job.objects.filter(pk=instance.active_job_id, status__in=ACTIVE_STATUSES).only("kind").first()
        job_kind = job.kind if job else None
    before = (instance.status, instance.status_detail, instance.container_state, instance.health)
    newly_installed = False
    adopt_spec_hash = ""
    if not agent_ok:
        instance.status, instance.status_detail = Status.UNKNOWN, (agent_error or "Node agent unreachable")[:500]
    else:
        restarts = crash_restarts(instance, container) if not job_kind else 0
        unexpected = policy_restarts(instance, container)
        status, detail = state.compute(instance, container, job_kind, crash_restarts=restarts)
        if (
            status == Status.STOPPED
            and not job_kind
            and instance.desired_state != "running"
            and before[0] == Status.FAILED
            and before[1].startswith(AUTO_STOP_PREFIX)
        ):
            # keep explaining why the server was stopped until the user starts it again
            status, detail = Status.FAILED, before[1]
        instance.status, instance.status_detail = status, detail[:500]
        if restarts >= CRASH_LOOP_AUTOSTOP and instance.desired_state == "running":
            _stop_crash_loop(instance, restarts)
            status = instance.status
        elif unexpected and not job_kind and instance.desired_state == "running":
            _notify_unexpected_restart(instance, unexpected)
        c = container or {}
        instance.container_state = c.get("status") or ""
        instance.health = c.get("health") or ""
        instance.last_exit_code = c.get("exit_code")
        instance.oom_killed = bool(c.get("oom_killed"))
        instance.started_at = _ts(c.get("started_at")) if c.get("running") else instance.started_at
        if c.get("spec_hash") and not instance.container_spec_hash:
            adopt_spec_hash = instance.container_spec_hash = c["spec_hash"]
        if status == Status.RUNNING:
            now = timezone.now()
            if instance.ready_at is None or (instance.started_at and instance.ready_at < instance.started_at):
                instance.ready_at = now
            if instance.installed_at is None:
                instance.installed_at = now
                newly_installed = True
        elif status not in (Status.RUNNING, Status.BACKING_UP):
            instance.ready_at = (
                None if status in (Status.STOPPED, Status.FAILED, Status.STARTING, Status.INSTALLING) else instance.ready_at
            )
    after = (instance.status, instance.status_detail, instance.container_state, instance.health)
    instance.status_updated_at = timezone.now()
    same = Instance.objects.filter(pk=instance.pk, node_id=instance.node_id, archived_at__isnull=instance.archived_at is None)
    if not same.update(**{f: getattr(instance, f) for f in OBSERVED_FIELDS}):
        return False  # moved, archived or deleted meanwhile: this observation is stale
    if adopt_spec_hash:
        same.filter(container_spec_hash="").update(container_spec_hash=adopt_spec_hash)
    if newly_installed and same.filter(installed_at__isnull=True).update(installed_at=instance.installed_at):
        try:
            from apps.modpacks.pinning import pin_installed_release

            pin_installed_release(instance)
        except Exception as exc:  # pinning is best effort; never break reconciliation
            log.warning("pinning failed for %s: %s", instance.pk, exc)
    if before != after:
        broadcast("instance.status", instance_payload(instance), str(instance.id))
        _notify_transition(instance, before[0], after[0])
        return True
    return False


def _notify_transition(instance: Instance, old: str, new: str) -> None:
    if old == new:
        return
    from apps.notifications.service import notify

    data = {"status": new, "detail": instance.status_detail}
    if new == Status.FAILED:
        _notify_failed(instance, f"{instance.name} failed", instance.status_detail or "The server failed.", data)
    elif new == Status.RUNNING and old in (
        Status.STARTING,
        Status.INSTALLING,
        Status.RESTARTING,
        Status.UPDATING,
        Status.CREATED,
        Status.STOPPED,
        Status.UNHEALTHY,
    ):
        notify(
            "instance.running", f"{instance.name} is online", "The server is accepting connections.", instance=instance, data=data
        )
    elif new == Status.STOPPED and old in (Status.RUNNING, Status.STOPPING, Status.UNHEALTHY):
        notify(
            "instance.stopped",
            f"{instance.name} stopped",
            instance.status_detail or "The server stopped.",
            instance=instance,
            data=data,
        )


def _agent_problem(exc: ApiError) -> str:
    if exc.error_code == "docker_unavailable":
        return "Docker is not reachable on this node (state unknown until it is back)"
    if exc.error_code == "agent_unavailable":
        return "Node agent unreachable (state unknown until it is back)"
    return f"Node agent error: {exc.detail}"


def reconcile_instance(instance_id: Any) -> Instance | None:
    instance = Instance.objects.select_related("node").filter(pk=instance_id).first()
    if instance is None:
        return None
    if instance.archived_at:
        apply_observation(instance, None)
        return instance
    try:
        container = AgentClient(instance.node).status(instance.id)
    except ApiError as exc:
        log.warning("reconcile %s: agent error %s", instance_id, exc)
        apply_observation(instance, None, agent_ok=False, agent_error=_agent_problem(exc))
        return instance
    apply_observation(instance, container)
    return instance


def reconcile_node(node: Node) -> dict[str, int]:
    instances = list(Instance.objects.filter(node=node))
    try:
        containers = AgentClient(node).list_containers()
    except ApiError as exc:
        node.last_error = str(exc)[:1000]
        node.save(update_fields=["last_error"])
        for inst in instances:
            if not inst.archived_at:
                apply_observation(inst, None, agent_ok=False, agent_error=_agent_problem(exc))
        return {"instances": len(instances), "changed": 0, "orphans": 0}
    node.last_seen_at = timezone.now()
    node.last_error = ""
    node.save(update_fields=["last_seen_at", "last_error"])
    by_id = {c.get("instance_id"): c for c in containers}
    changed = 0
    for inst in instances:
        c = by_id.pop(str(inst.id), None)
        if apply_observation(inst, c if not inst.archived_at else None):
            changed += 1
    if by_id:
        log.info("Node %s has %d managed containers without a panel instance: %s", node.name, len(by_id), list(by_id))
    return {"instances": len(instances), "changed": changed, "orphans": len(by_id)}
