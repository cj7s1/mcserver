"""Observed-state computation: maps container + job state to a user-facing status.

A running container is *not* a ready server: readiness comes from the image's
``mc-health`` health check (a real Minecraft status ping).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import Instance, Status

JOB_STATUS = {
    "stop": Status.STOPPING,
    "restart": Status.RESTARTING,
    "backup": Status.BACKING_UP,
    "restore": Status.RESTORING,
    "restore_path": Status.RESTORING,
    "snapshot_restore": Status.RESTORING,
    "modpack_rollback": Status.RESTORING,
    "world_replace": Status.RESTORING,
    "world_reset": Status.RESTORING,
    "modpack_update": Status.UPDATING,
    "software_update": Status.UPDATING,
    "recreate": Status.UPDATING,
    "migrate": Status.UPDATING,
    "install": Status.INSTALLING,
    "delete": Status.STOPPING,
}

STARTUP_GRACE = timedelta(minutes=10)
OOM_DETAIL = "Killed: out of memory. Increase the memory allocation."
CRASH_LOOP_RESTARTS = 3


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return parse_datetime(ts.replace("Z", "+00:00")[:32]) or None
    except ValueError:
        return None


def compute(
    instance: Instance,
    container: dict[str, Any] | None,
    job_kind: str | None = None,
    now: datetime | None = None,
    crash_restarts: int = 0,
) -> tuple[str, str]:
    """Return (status, detail).

    ``crash_restarts`` is how many times Docker restarted the container since it was last
    ready (tracked by the reconciler); a high value while not healthy is a crash loop.
    """
    now = now or timezone.now()
    if instance.archived_at:
        return Status.ARCHIVED, "Archived; container removed, data kept"
    if job_kind in JOB_STATUS and not (job_kind == "backup" and not (container or {}).get("running")):
        return JOB_STATUS[job_kind], ""

    if not container or not container.get("exists"):
        # A stopped server has no container: the agent kept a record of its last run
        # (reconcile.maybe_retire). That run is over and was meant to end, so only what still
        # needs attention is carried over from it: the out-of-memory kill.
        last_run = (container or {}).get("last_run") or None
        if last_run:
            if last_run.get("oom_killed"):
                return Status.FAILED, OOM_DETAIL
            return Status.STOPPED, ""
        if instance.installed_at is None:
            # never installed, whatever was applied: a container that was created and removed
            # again without a start (create without starting, clone) does not change that
            return Status.CREATED, "Not installed yet"
        return Status.STOPPED, "No container (will be created on start)"

    state = container.get("status")
    health = container.get("health")
    exit_code = container.get("exit_code")
    if crash_restarts >= CRASH_LOOP_RESTARTS and health != "healthy" and state in ("running", "restarting"):
        return Status.FAILED, (
            f"Crash loop: the server exited during startup {crash_restarts} times. "
            "See the Overview diagnostics and the console for the cause."
        )
    if container.get("restarting") or state == "restarting":
        return Status.FAILED, f"Crash loop: container is restarting (exit code {exit_code})"
    if state == "running":
        if health == "healthy":
            return Status.RUNNING, ""
        if health is None:
            # health check disabled/unsupported: treat a running container as running
            return Status.RUNNING, "Health check unavailable; readiness not verified"
        started = _parse(container.get("started_at"))
        if instance.installed_at is None:
            return Status.INSTALLING, "Installing server software (first start can take several minutes)"
        if health == "starting" or (started and now - started < STARTUP_GRACE):
            return Status.STARTING, "Waiting for the server to accept connections"
        return Status.UNHEALTHY, "Server process is running but not answering status pings"
    if state in ("created", "exited", "dead"):
        if container.get("oom_killed"):
            return Status.FAILED, OOM_DETAIL
        if instance.resume_after_boot and instance.desired_state == "running" and state == "exited":
            # stopped by `manage.py host_shutdown`; scheduling.resume_flagged_servers starts it again
            # (only while it is still meant to run, and it drops a mark it will not act on)
            unclean = "" if exit_code in (0, None) else f" (exit code {exit_code}: not a clean stop)"
            return Status.STOPPED, f"Stopped because the host was shutting down{unclean}; it is started again automatically"
        if instance.desired_state == "running" and exit_code not in (0, None):
            return Status.FAILED, f"Server exited unexpectedly (exit code {exit_code}). Check the console log."
        if instance.desired_state == "running" and exit_code == 0 and state == "exited":
            return Status.STOPPED, "Server stopped itself"
        if instance.installed_at is None and state == "created":
            return Status.CREATED, "Installed container; not started yet"
        return Status.STOPPED, ""
    if state == "paused":
        return Status.STOPPED, "Container paused"
    return Status.UNKNOWN, f"Unexpected container state '{state}'"
