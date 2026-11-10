"""Reconcile observed container state into the database (runs every few seconds)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.console.events import broadcast
from apps.core.errors import ApiError
from apps.jobs.models import ACTIVE_STATUSES, Job
from apps.nodes.client import AgentClient
from apps.nodes.models import Node

from . import state
from .models import DesiredState, Instance, Status

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
    # The server stays down until someone starts it: that also settles a pending "start again
    # after the host restart" mark (host_shutdown), or the resume step would start it once more.
    Instance.objects.filter(pk=instance.pk).update(desired_state="stopped", resume_after_boot=False, resume_flagged_at=None)
    instance.desired_state = "stopped"
    instance.resume_after_boot = False
    instance.resume_flagged_at = None
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
        # Without a container, how the last run ended comes from the record the agent kept of it
        # (a stopped server has no container, see maybe_retire). No record: nothing is known.
        ended = c if c.get("exists") else (c.get("last_run") or {})
        instance.container_state = c.get("status") or ""
        instance.health = c.get("health") or ""
        instance.last_exit_code = ended.get("exit_code")
        instance.oom_killed = bool(ended.get("oom_killed"))
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


# --- no container for a stopped server ----------------------------------------------------------
# A container is only needed while a server runs. The reconciler asks the agent to "retire" the
# container of a server that is meant to be stopped: the agent saves the last-run record (exit
# code, out-of-memory flag, restart count, times, last health output, image id, the end of the
# console) and then removes the container. Nothing else removes it: Stop, Kill, the crash-loop
# stop, a stop job that was cut off and a server created without starting all end here, on the
# next pass. A server that died while it is meant to run keeps its container as evidence until
# someone presses Start or Stop.
RETIRE_FEATURE = "retire"  # the agent's name for it in GET /v1/info ("features")
# How long a container must have been stopped. A job that stops a server and starts it again
# holds the server meanwhile; this covers what does not: the moment between a job's last
# request and the release of its lock, and a kill sent while a stop or restart is in progress.
# The agent checks it again against the Docker host's own clock.
RETIRE_SETTLE_SECONDS = 10.0
RETIRABLE_STATES = ("created", "exited", "dead")
# Answers of the agent that mean "not now" (the next pass looks again), not a failure.
RETIRE_NOT_NOW = frozenset({"container_running", "not_settled", "container_busy", "instance_busy"})
# A retire that keeps failing is written to the audit log at most this often per server (seconds).
RETIRE_FAILURE_AUDIT_INTERVAL = 3600


def retire_enabled() -> bool:
    return bool(getattr(settings, "MCPANEL_RETIRE_STOPPED_CONTAINERS", True))


def retire_blocker(
    instance: Instance, container: dict[str, Any] | None, *, now: datetime | None = None, settle: float | None = None
) -> str | None:
    """Why the container of this server is NOT retired now; None when it is to be retired.

    The rule: the container exists and is not running, restarting or paused, it has been in that
    state for the settle time, and the server is not archived, is meant to be stopped
    (``desired_state``) and no job is queued or running for it. The server's row is read again
    here: the caller's copy may be older than a job that took the server meanwhile.
    """
    c = container or {}
    if not c.get("exists"):
        return "no_container"
    if c.get("running") or c.get("restarting") or c.get("status") not in RETIRABLE_STATES:
        return "running"
    settle = RETIRE_SETTLE_SECONDS if settle is None else settle
    since = (_ts(c.get("finished_at")) if c.get("started_at") else None) or _ts(c.get("created_at"))
    if since is not None and ((now or timezone.now()) - since).total_seconds() < settle:
        return "not_settled"
    fresh = Instance.objects.filter(pk=instance.pk).values("node_id", "archived_at", "desired_state", "active_job_id").first()
    if fresh is None or fresh["node_id"] != instance.node_id:
        return "moved"
    if fresh["archived_at"]:
        return "archived"
    if fresh["desired_state"] != DesiredState.STOPPED:
        return "meant_to_run"
    busy = Job.objects.filter(status__in=ACTIVE_STATUSES)
    if busy.filter(instance_id=instance.pk).exists() or (
        fresh["active_job_id"] and busy.filter(pk=fresh["active_job_id"]).exists()
    ):
        return "job_active"
    return None


def retire_container(
    instance: Instance, client: AgentClient | None = None, *, settle: float | None = None
) -> dict[str, Any] | None:
    """Ask the agent to retire the server's container. Returns the agent's answer, or None when
    it did not happen (the agent said "not now", or it failed: logged and audited)."""
    from apps.audit import service as audit

    client = client or AgentClient(instance.node)
    try:
        answer = client.retire(instance.id, settle_seconds=RETIRE_SETTLE_SECONDS if settle is None else settle)
    except ApiError as exc:
        if exc.error_code in RETIRE_NOT_NOW:
            log.debug("container of %s not retired now: %s", instance.pk, exc.detail)
            return None
        # e.g. last_run_failed: the record could not be written, so the agent kept the container
        log.warning("could not retire the container of %s (%s): %s", instance.name, instance.pk, exc.detail)
        if cache.add(f"retire-failed:{instance.pk}", 1, RETIRE_FAILURE_AUDIT_INTERVAL):
            audit.record(
                "instance.container_retire_failed",
                instance=instance,
                target=instance,
                success=False,
                details={"error": str(exc.detail)[:300], "code": exc.error_code},
            )
        return None
    if answer.get("retired"):
        last_run = answer.get("last_run") or {}
        log.info("retired the container of stopped server %s (%s)", instance.name, instance.pk)
        audit.record(
            "instance.container_retired",
            instance=instance,
            target=instance,
            details={
                "recorded": bool(answer.get("recorded")),
                "exit_code": last_run.get("exit_code") if answer.get("recorded") else None,
                "console_lines": last_run.get("log_lines") if answer.get("recorded") else None,
            },
        )
    return answer


def maybe_retire(instance: Instance, container: dict[str, Any] | None) -> bool:
    """The retire step of a reconcile pass. Returns True when the container is gone now.

    Does nothing unless MCPANEL_RETIRE_STOPPED_CONTAINERS is on, the rule of ``retire_blocker``
    holds and the node's agent offers the operation (an older agent keeps its containers, as
    before). Never raises: a problem here must not stop the reconciliation of other servers.
    """
    try:
        if not retire_enabled() or retire_blocker(instance, container) is not None:
            return False
        client = AgentClient(instance.node)
        if not client.supports(RETIRE_FEATURE):
            return False
        answer = retire_container(instance, client)
        if answer is None:
            return False
        observed: dict[str, Any] = {"exists": False}
        if answer.get("last_run"):
            observed["last_run"] = answer["last_run"]
        apply_observation(instance, observed)
        return True
    except Exception:
        log.exception("retiring the container of %s failed", instance.pk)
        return False


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
    maybe_retire(instance, container)
    return instance


def reconcile_node(node: Node) -> dict[str, int]:
    instances = list(Instance.objects.filter(node=node))
    try:
        listed = AgentClient(node).list_state()
        containers = listed["containers"]
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
    last_runs = listed["last_runs"]
    changed = 0
    for inst in instances:
        c = by_id.pop(str(inst.id), None)
        if inst.archived_at:
            c = None
        elif c is None and last_runs.get(str(inst.id)):
            c = {"exists": False, "last_run": last_runs[str(inst.id)]}  # stopped: its container was retired
        was_changed = apply_observation(inst, c)
        if maybe_retire(inst, c) or was_changed:
            changed += 1
    if by_id:
        log.info("Node %s has %d managed containers without a panel instance: %s", node.name, len(by_id), list(by_id))
    return {"instances": len(instances), "changed": changed, "orphans": len(by_id)}
