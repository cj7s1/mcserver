"""Stop every running server gracefully before the host stops Docker.

    docker exec <panel container> python manage.py host_shutdown [--timeout 45] [--node <name>]

Run by the Unraid hook ``deploy/unraid/stopping_docker`` right before Unraid stops Docker
(array stop, reboot, shutdown); see docs/UNRAID-SHUTDOWN.md. When Docker itself stops a server
it gets a few seconds; here it gets up to ``--timeout`` (capped at its own stop timeout), and
the agent saves the world first.

For every server of the node that is not archived and whose container runs:

* ``resume_after_boot`` is set (``desired_state`` is left alone: the server is meant to run).
  The scheduler tick of the panel that starts after Docker came back starts these servers
  again, one per minute (``apps.instances.scheduling.resume_flagged_servers``);
* the agent is asked to stop it, the same graceful stop the panel's Stop button uses.

All servers are stopped in parallel and the command returns within ``--timeout`` plus
``MARGIN_SECONDS`` whatever the agent does. It talks to the database and the agent only: no
job is queued, so it needs no Celery worker and no Redis, and it does not wait for a job that
holds a server. One line is printed and one audit entry written per server. Servers that are
not running are left alone and are not marked.

Exit status: 0, unless nothing could be reached at all (unknown node, or the agent does not
answer). The hook then stops the containers with plain ``docker stop``.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.audit import service as audit
from apps.core.errors import ApiError
from apps.instances.models import Instance
from apps.nodes.client import AgentClient, AgentError
from apps.nodes.models import Node

# The command returns at most this long after --timeout ran out (answers still open are
# reported as "no answer"; the agent goes on stopping those servers).
MARGIN_SECONDS = 8.0
# Asking the agent which containers run must not eat the time the servers need to stop.
LIST_TIMEOUT = 10.0


def _live(container: dict[str, Any]) -> bool:
    return bool(container.get("running") or container.get("restarting") or container.get("status") == "restarting")


class Command(BaseCommand):
    help = "Stop all running servers of a node before the host stops Docker and mark them to be started again after boot"

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument(
            "--timeout",
            type=int,
            default=45,
            help="Seconds a server gets to stop (never more than its own stop timeout). Default 45.",
        )
        parser.add_argument("--node", default="", help="Name of the node (default: the default node)")

    def handle(self, *args, **opts):  # type: ignore[no-untyped-def]
        budget = int(opts["timeout"])
        if not 1 <= budget <= 900:
            raise CommandError("--timeout must be between 1 and 900 seconds")
        deadline = time.monotonic() + budget + MARGIN_SECONDS
        node = self._node(opts["node"])

        # One client for everything: its TLS trust is resolved by this first call, so the
        # threads below only make HTTP requests and never touch the database.
        client = AgentClient(node, timeout=min(LIST_TIMEOUT, float(budget)))
        try:
            containers = client.list_containers()
        except ApiError as exc:
            raise CommandError(
                f"host_shutdown: the agent of node '{node.name}' could not be reached ({exc.detail}); nothing was stopped"
            ) from exc
        live = {str(c.get("instance_id")) for c in containers if _live(c)}
        servers = list(Instance.objects.filter(node=node, archived_at__isnull=True).order_by("name"))
        targets = [inst for inst in servers if str(inst.id) in live]
        idle = len(servers) - len(targets)
        unknown = len(live - {str(inst.id) for inst in servers})
        if unknown:
            self._say(f"host_shutdown: {unknown} running container(s) without a server in the panel were left to Docker")
        if not targets:
            self._say(f"host_shutdown: no running servers on node '{node.name}' ({idle} not running, left alone)")
            return

        # Mark first, in one statement: whatever happens to this command afterwards, the
        # servers that were running come back after the next boot.
        Instance.objects.filter(pk__in=[inst.pk for inst in targets]).update(
            resume_after_boot=True, resume_flagged_at=timezone.now()
        )

        answers: queue.Queue[tuple[Instance, float, dict[str, Any] | None, Exception | None]] = queue.Queue()

        def stop(inst: Instance, seconds: int) -> None:
            began = time.monotonic()
            status: dict[str, Any] | None = None
            error: Exception | None = None
            try:
                status = client.stop(inst.id, timeout=seconds)
            except Exception as exc:  # reported per server by the main thread
                error = exc
            answers.put((inst, time.monotonic() - began, status, error))

        allowed: dict[Any, int] = {}
        for inst in targets:
            allowed[inst.pk] = max(1, min(int(inst.stop_timeout or budget), budget))
            # daemon threads: a request that outlives the deadline must not keep the command alive
            threading.Thread(target=stop, args=(inst, allowed[inst.pk]), name=f"host-shutdown-{inst.pk}", daemon=True).start()

        waiting = {inst.pk: inst for inst in targets}
        counts = {"stopped": 0, "failed": 0, "no_answer": 0}
        unreachable = 0
        while waiting:
            try:
                inst, took, status, error = answers.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                break
            waiting.pop(inst.pk, None)
            details: dict[str, Any] = {"timeout": allowed[inst.pk], "seconds": round(took, 1)}
            if error is None:
                exit_code = (status or {}).get("exit_code")
                still_running = bool((status or {}).get("running"))
                details.update(result="stopped", exit_code=exit_code)
                if still_running:
                    counts["failed"] += 1
                    details["result"] = "failed"
                    self._say(f"FAILED    {inst.name}: the agent answered after {took:.0f} s but the container still runs")
                else:
                    counts["stopped"] += 1
                    # 0 = the server shut down by itself; 137/255 = it was killed when the time ran out
                    how = "" if exit_code in (0, None) else f" (exit code {exit_code}: not a clean stop)"
                    self._say(f"stopped   {inst.name} in {took:.0f} s{how}")
                self._audit(inst, success=not still_running, details=details)
                continue
            counts["failed"] += 1
            if not isinstance(error, AgentError):
                unreachable += 1  # no answer from the agent itself (connection refused, TLS, token, ...)
            message = str(error.detail) if isinstance(error, ApiError) else f"{type(error).__name__}: {error}"
            details.update(result="failed", error=message[:300])
            self._say(f"FAILED    {inst.name}: {message}")
            self._audit(inst, success=False, details=details)
        for inst in waiting.values():
            counts["no_answer"] += 1
            limit = budget + MARGIN_SECONDS
            self._say(f"NO ANSWER {inst.name}: still stopping after {limit:.0f} s (the agent goes on with it)")
            self._audit(
                inst, success=False, details={"timeout": allowed[inst.pk], "seconds": round(limit, 1), "result": "no_answer"}
            )

        self._say(
            f"host_shutdown: node '{node.name}': {counts['stopped']} stopped, {counts['failed']} failed, "
            f"{counts['no_answer']} without an answer, {idle} not running (left alone). "
            f"{len(targets)} marked to be started again after the next boot."
        )
        if unreachable == len(targets):
            raise CommandError(f"host_shutdown: the agent of node '{node.name}' stopped answering; no server was stopped")

    def _node(self, name: str) -> Node:
        if not name:
            return Node.default()
        node = Node.objects.filter(name=name).first()
        if node is None:
            raise CommandError(f"host_shutdown: there is no node named '{name}'")
        return node

    def _say(self, line: str) -> None:
        self.stdout.write(line)
        self.stdout.flush()  # the hook logs line by line while this runs

    def _audit(self, inst: Instance, *, success: bool, details: dict[str, Any]) -> None:
        try:
            # no actor: the audit log shows these as done by the system
            audit.record("instance.stopped_for_host_shutdown", instance=inst, target=inst, success=success, details=details)
        except Exception as exc:  # the stop matters more than its audit entry
            self.stderr.write(f"host_shutdown: could not write the audit entry for {inst.name}: {exc}")
