"""Remove the containers of servers that are meant to be stopped, once and by hand.

    docker compose exec panel python manage.py retire_stopped_containers --dry-run
    docker compose exec panel python manage.py retire_stopped_containers [--node <name>]

A stopped server needs no container. With MCPANEL_RETIRE_STOPPED_CONTAINERS on (the default) the
reconciler removes them by itself, about 10 to 20 seconds after a server stopped
(``apps.instances.reconcile.maybe_retire``). This command applies the same rule to every server
at once and prints what it decided per server, for the first roll-out on a host that still has
the exited containers of older releases:

* ``--dry-run`` changes nothing and lists which containers would be removed and which are kept,
  with the reason;
* without it the listed containers are removed now. That also works while the setting is off
  (a single manual sweep), and never touches a server that is running, meant to run, archived
  or held by a job.

For every container that is removed the agent first saves the record of the server's last run
(exit code, the end of the console output, ...). If that fails the container is kept and the
line says so. Nodes whose agent is older than this panel cannot do this and are skipped.

Exit status: 0, unless a node could not be asked at all.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.core.errors import ApiError
from apps.instances.models import Instance
from apps.instances.reconcile import (
    RETIRE_FEATURE,
    RETIRE_SETTLE_SECONDS,
    _ts,
    apply_observation,
    retire_blocker,
    retire_container,
    retire_enabled,
)
from apps.nodes.client import AgentClient
from apps.nodes.models import Node


def _age(container: dict[str, Any]) -> str:
    since = (_ts(container.get("finished_at")) if container.get("started_at") else None) or _ts(container.get("created_at"))
    if since is None:
        return ""
    seconds = max(0, int((timezone.now() - since).total_seconds()))
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return f"{seconds} s ago"


def _describe(container: dict[str, Any]) -> str:
    """What the container is, in a few words: "exited 3 days ago, exit code 0"."""
    state = container.get("status") or "unknown"
    if state == "created":
        return f"created {_age(container)}, never started".replace("  ", " ")
    parts = [f"{state} {_age(container)}".strip()]
    if container.get("exit_code") is not None:
        parts.append(f"exit code {container['exit_code']}")
    if container.get("oom_killed"):
        parts.append("out of memory")
    return ", ".join(parts)


def _why_kept(reason: str, instance: Instance, container: dict[str, Any]) -> str:
    if reason == "running":
        return f"its container is {container.get('status') or 'running'}"
    if reason == "not_settled":
        return f"it stopped less than {RETIRE_SETTLE_SECONDS:.0f} s ago ({_describe(container)})"
    if reason == "archived":
        return f"the server is archived ({_describe(container)})"
    if reason == "job_active":
        return f"a job is queued or running for it ({_describe(container)})"
    if reason == "meant_to_run":
        if instance.resume_after_boot:
            return f"stopped for the host shutdown; it is started again automatically ({_describe(container)})"
        return f"the server is meant to run but is not running ({_describe(container)}): press Start, or Stop to dismiss it"
    return reason


class Command(BaseCommand):
    help = "Remove the containers of stopped servers (the agent keeps a record of the last run); --dry-run only lists them"

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("--dry-run", action="store_true", help="Change nothing; list what would be removed and what is kept")
        parser.add_argument("--node", default="", help="Only this node (default: every node)")

    def handle(self, *args, **opts):  # type: ignore[no-untyped-def]
        dry_run = bool(opts["dry_run"])
        nodes = list(Node.objects.filter(name=opts["node"]) if opts["node"] else Node.objects.all())
        if opts["node"] and not nodes:
            raise CommandError(f"There is no node named '{opts['node']}'")
        self._say(
            "MCPANEL_RETIRE_STOPPED_CONTAINERS is "
            + (
                "on: the panel also removes the containers of stopped servers by itself."
                if retire_enabled()
                else "off: the panel keeps the containers of stopped servers; this command removes them once."
            )
        )
        totals = {"retired": 0, "would_retire": 0, "kept": 0, "failed": 0, "none": 0}
        unreachable = []
        for node in nodes:
            try:
                self._node(node, dry_run, totals)
            except ApiError as exc:
                unreachable.append(node.name)
                self._say(f"node '{node.name}': the agent could not be asked ({exc.detail}); nothing was changed there")
        done = (
            f"{totals['would_retire']} container(s) would be removed" if dry_run else f"{totals['retired']} container(s) removed"
        )
        self._say(
            f"{done}, {totals['kept']} kept, {totals['none']} server(s) without a container"
            + (f", {totals['failed']} could not be removed (see above)" if totals["failed"] else "")
            + "."
        )
        if unreachable:
            raise CommandError(f"Could not ask the agent of: {', '.join(unreachable)}")

    def _node(self, node: Node, dry_run: bool, totals: dict[str, int]) -> None:
        client = AgentClient(node)
        listed = client.list_state()
        supported = RETIRE_FEATURE in client.features(refresh=True)
        self._say(
            f"node '{node.name}':"
            + ("" if supported else " its agent is older than this panel and cannot remove containers this way; update it first")
        )
        by_id = {str(c.get("instance_id")): c for c in listed["containers"]}
        for inst in Instance.objects.filter(node=node).select_related("node").order_by("name"):
            container = by_id.pop(str(inst.id), None)
            if container is None:
                totals["none"] += 1
                continue
            reason = retire_blocker(inst, container)
            if reason is not None:
                totals["kept"] += 1
                self._say(f"  keep          {inst.name}: {_why_kept(reason, inst, container)}")
                continue
            if dry_run or not supported:
                totals["would_retire"] += 1
                self._say(f"  would remove  {inst.name}: {_describe(container)}")
                continue
            answer = retire_container(inst, client)
            if answer is None:
                totals["failed"] += 1
                self._say(f"  NOT removed   {inst.name}: the agent refused or failed (see the panel log and the audit log)")
                continue
            totals["retired"] += 1
            observed: dict[str, Any] = {"exists": False}
            if answer.get("last_run"):
                observed["last_run"] = answer["last_run"]
            apply_observation(inst, observed)
            kept = (
                f"last run saved ({(answer['last_run'] or {}).get('log_lines', 0)} console lines)"
                if answer.get("recorded")
                else "never started, nothing to save"
            )
            self._say(f"  removed       {inst.name}: {_describe(container)}; {kept}")
        if by_id:
            self._say(f"  {len(by_id)} container(s) without a server in the panel were left alone")

    def _say(self, line: str) -> None:
        self.stdout.write(line)
