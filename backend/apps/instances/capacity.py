"""Memory admission: does one more server fit into the RAM of its node?

Every server's container has a memory limit (``Instance.container_memory_mb``), and the JVM
commits its whole heap right at the start (MEMORY sets -Xms and -Xmx, Aikar's flags pre-touch
it). Before a start that creates or starts a container, the limits of the servers whose
containers run on the node plus the new one are compared with the node's RAM (``docker.
memory_bytes`` of the agent's GET /v1/info) minus a reserve for everything else on the host
(system setting ``memory_reserve_mb``, default DEFAULT_RESERVE_MB).

* A start a user asked for fails with 409 ``insufficient_memory`` and the numbers, unless the
  request says ``allow_overcommit`` (``services.power``; the job then skips the check too).
* The resume step after a host restart and scheduled starts skip the server for this run and
  try again later (``scheduling``).
* A server whose container runs already with its current configuration is never refused: it is
  counted already. Stops are never refused.
* A server that a job stopped for its work (a restore, an update) is started again without the
  check: it was running when the job began.

When the node's RAM or its containers cannot be read, the start is not held up: nothing is known.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from django.core.cache import cache

from apps.core.errors import ApiError, ConflictError
from apps.core.models import SystemSetting

log = logging.getLogger(__name__)

DEFAULT_RESERVE_MB = 4096
RESERVE_SETTING = "memory_reserve_mb"
NODE_MEMORY_TTL = 300  # seconds the RAM of a node is remembered
INFO_TIMEOUT = 5.0  # a form that is saved must not wait long for an agent that is down


def reserve_mb() -> int:
    value = SystemSetting.get(RESERVE_SETTING)
    try:
        return max(0, int(value)) if value is not None else DEFAULT_RESERVE_MB
    except (TypeError, ValueError):
        return DEFAULT_RESERVE_MB


def node_memory_mb(node: Any, client: Any = None) -> int | None:
    """Total RAM of the node in MB as its Docker daemon reports it, or None when it is not known."""
    from apps.nodes.client import AgentClient

    key = f"node-memory-mb:{node.pk}"
    cached = cache.get(key)
    if cached is not None:
        return int(cached) or None
    try:
        info = (client or AgentClient(node, timeout=INFO_TIMEOUT)).info()
    except ApiError as exc:
        log.info("the RAM of node %s cannot be read: %s", getattr(node, "name", node), exc)
        return None
    total = (info.get("docker") or {}).get("memory_bytes")
    mb = int(total) // (1024 * 1024) if isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0 else 0
    cache.set(key, mb, NODE_MEMORY_TTL)
    return mb or None


@dataclass
class Budget:
    node: str
    host_mb: int
    reserve_mb: int
    need_mb: int
    running_mb: int
    running: list[dict[str, Any]] = field(default_factory=list)

    @property
    def available_mb(self) -> int:
        return self.host_mb - self.reserve_mb

    @property
    def free_mb(self) -> int:
        return self.available_mb - self.running_mb

    @property
    def fits(self) -> bool:
        return self.running_mb + self.need_mb <= self.available_mb

    def details(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "host_mb": self.host_mb,
            "reserve_mb": self.reserve_mb,
            "available_mb": self.available_mb,
            "running_mb": self.running_mb,
            "need_mb": self.need_mb,
            "free_mb": self.free_mb,
            "running": self.running,
        }

    def message(self, name: str) -> str:
        return (
            f"Not enough memory on node '{self.node}' to start '{name}': its container needs {self.need_mb} MB, "
            f"the {len(self.running)} server(s) running there have {self.running_mb} MB, and of the node's "
            f"{self.host_mb} MB, {self.reserve_mb} MB are kept free for the host (setting {RESERVE_SETTING}), "
            f"so {max(self.free_mb, 0)} MB are left."
        )


def budget(instance: Any, *, client: Any = None) -> Budget | None:
    """The memory budget for starting ``instance`` now; None when it cannot be judged or the
    server's container runs already with its current configuration."""
    from apps.nodes.client import AgentClient

    from .models import Instance
    from .spec import spec_outdated

    client = client or AgentClient(instance.node)
    host = node_memory_mb(instance.node, client)
    if not host:
        return None
    try:
        containers = client.list_containers()
    except ApiError as exc:
        log.info("the containers of node %s cannot be listed: %s", instance.node.name, exc)
        return None
    live = {
        str(c.get("instance_id"))
        for c in containers
        if _is_uuid(c.get("instance_id")) and (c.get("running") or c.get("restarting") or c.get("status") == "restarting")
    }
    if str(instance.pk) in live and not spec_outdated(instance):
        return None  # it runs already as it is: counted
    live.discard(str(instance.pk))
    others = Instance.objects.filter(pk__in=list(live), node_id=instance.node_id).order_by("name")
    running = [{"id": str(o.pk), "name": o.name, "memory_mb": o.container_memory_mb} for o in others]
    return Budget(
        node=instance.node.name,
        host_mb=host,
        reserve_mb=reserve_mb(),
        need_mb=instance.container_memory_mb,
        running_mb=sum(r["memory_mb"] for r in running),
        running=running,
    )


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def insufficient(instance: Any, *, client: Any = None) -> Budget | None:
    """The budget when starting ``instance`` would not fit, else None."""
    found = budget(instance, client=client)
    return found if found is not None and not found.fits else None


def admit(instance: Any, *, client: Any = None) -> None:
    """Raise 409 ``insufficient_memory`` when starting ``instance`` would not fit."""
    short = insufficient(instance, client=client)
    if short is not None:
        raise ConflictError(
            short.message(instance.name)
            + " Stop another server first, lower the memory of this one, or start it anyway (allow_overcommit).",
            code="insufficient_memory",
            details=short.details(),
        )


def runs_now(instance: Any) -> bool:
    """Whether the server process runs as far as the panel knows (also while a job makes its status "backing up")."""
    return instance.container_state == "running" or instance.status in ("running", "starting", "unhealthy")


def on_node(instance: Any, node: Any) -> Any:
    """A copy of ``instance`` placed on ``node`` (unsaved), to judge a start there (migration)."""
    import copy

    probe = copy.copy(instance)
    probe.node = node
    return probe


# who may start a server that does not fit into the node's memory (``allow_overcommit``, D-4/I-5): the
# memory reserve is the administrator's setting, so overriding it is too
OVERCOMMIT_PERMISSION = "system.settings"


def require_overcommit_right(user: Any) -> None:
    """403 ``overcommit_not_allowed`` unless ``user`` (None: the panel itself) holds OVERCOMMIT_PERMISSION."""
    if user is None or OVERCOMMIT_PERMISSION in user.global_permissions():
        return
    raise ApiError(
        "Starting a server that does not fit into the node's memory needs an administrator (the global "
        "permission to change system settings); ask one, or stop another server first.",
        code="overcommit_not_allowed",
        status_code=403,
    )


def admit_request(instance: Any, *, starts: bool, allow_overcommit: Any, node: Any = None, user: Any = None) -> dict[str, Any]:
    """For a request that queues a job which starts the server (create with start, a software or
    modpack update with "start afterwards", a recreate of a running server, a migration of a
    running server to ``node``): 409 ``insufficient_memory`` now, with the numbers, so the user
    can confirm "Start anyway" like for a power start (frontend ``lib/power.ts``); with
    ``allow_overcommit`` the job params that make the job start it anyway (``handlers.admit_memory``),
    only for an administrator (``require_overcommit_right``: ``user`` is the requester)."""
    if not starts:
        return {}
    if allow_overcommit is True:
        require_overcommit_right(user)
        return {"allow_overcommit": True}
    admit(instance if node is None else on_node(instance, node))
    return {}


def container_limit_error(node: Any, container_mb: int) -> str | None:
    """Why one container of ``container_mb`` cannot run on ``node`` at all (more than its RAM), or None."""
    host = node_memory_mb(node)
    if host and container_mb > host:
        return (
            f"The server's container would need {container_mb} MB (heap plus overhead), but node '{node.name}' "
            f"has only {host} MB of RAM."
        )
    return None


def node_cpus(node: Any) -> int | None:
    """CPUs of the node as its Docker daemon reports them (``docker.cpus`` of GET /v1/info), or None."""
    from apps.nodes.client import AgentClient

    key = f"node-cpus:{node.pk}"
    cached = cache.get(key)
    if cached is not None:
        return int(cached) or None
    try:
        info = AgentClient(node, timeout=INFO_TIMEOUT).info()
    except ApiError as exc:
        log.info("the CPUs of node %s cannot be read: %s", getattr(node, "name", node), exc)
        return None
    cpus = (info.get("docker") or {}).get("cpus")
    count = int(cpus) if isinstance(cpus, (int, float)) and not isinstance(cpus, bool) and cpus > 0 else 0
    cache.set(key, count, NODE_MEMORY_TTL)
    return count or None


def cpu_limit_error(node: Any, cpu_limit: float | None) -> str | None:
    """Why ``cpu_limit`` cannot be used on ``node`` (more CPUs than it has: the agent refuses such a
    container with ``cpu_limit``), or None."""
    if not cpu_limit:
        return None
    cpus = node_cpus(node)
    if cpus and cpu_limit > cpus:
        return f"The CPU limit {cpu_limit:g} is more than the {cpus} CPUs of node '{node.name}'."
    return None
